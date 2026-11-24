"""HF / VoxCPM Pool — Phase 1 + Addendum Verification Suite (2026-11-24).

Covers all owner-mandated pins:
- Addendum (1-3): `DEFAULT_VOXCPM_SPACES == ["openbmb/VoxCPM-Demo"]`, third-party
  `yeyee2609/voxcpm2` & `soemintheinsmt6/myanmar-voxcpm2` absent from merged rotation,
  `yeyee2609/voxcpm` v1 excluded globally, CPU demo excluded from automated failover.
- (a) Park class identity across `core` & `app.services.recap_engine.core` in real
  `recap_pipeline` import order; end-to-end `run_recap_pipeline -> _execute_recap_background_job`
  raising `VoxCPMQuotaExhaustedError` at segment 2/3 -> `WAITING_CHOICE` with segment 1
  checkpointed + DM buttons; `segmented_mode` re-raises `RecapParkRequested` on part 2+.
- (b) Unconditional no-fallback on 7-segment clone recap + multi-chunk standalone clone
  under both `VoxCPMQuotaExhaustedError` and `TimeoutError` (ZERO Edge/persona calls);
  `dp:edgemix` retired; `dp:cancel` flips to `CANCELLED`, refunds 100%, frees slot.
- (c) `job.cancel()` called BEFORE `TimeoutError` on engine poll-timeout path.
- (d) Best-Fit picks smallest sufficient balance and never tries sub-gate tokens;
  pre-job gate rejects unfinishable job and admits fitting job.
- (e) Resumer reads shared `aj_hf:pool_state` and requires `effective_usable >= needed * 1.25`;
  24 h safety valve expires & refunds.
- (f) Standalone chunk-N resume reuses chunks `1..N-1` WAVs from `dp_refs/<job_id>/`
  without re-synthesis.
- (g) Billing invariance: net charge == 0 across FAILED / CANCELLED / PARK_EXPIRED,
  voice hold refunded on Recap `edge_all`, and single settlement on park -> resume -> SUCCESS.
- (h) All 4 watermark bands (`>40%`, `40%->20%`, `20%->8%`, `<8%`), including Free 12-seg
  recap held at 15% while Free 45 s clone runs, and Paid recap parking (not failing) at 5%.
- (i) `auth_dead` 24 h quarantine (`whoami-v2` confirmation, walk exclusion, Key Health
  surfacing, auto-clear on 200/env change, zero raw token storage) + `scripts/probe_hf_tokens.py`.
- (j) Recap VoxCPM workers == 2 sharing ONE `JobSpacePin`; own-voice recap ASR pre-pass
  invoked and skipped on `m2f`/`f2m`; `MAX_CONCURRENT_VOXCPM_JOBS == 2` + 2:1 fairness +
  15-min anti-starvation + 15-min worker host affinity.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import wave

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import worker as worker_mod
from app.config import settings
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Base, Job, MinuteBalance, User
from app.services import billing as billing_mod
from app.services import clone_prosody as cp_mod
from app.services import hf_pool_ledger as hf_ledger
from app.services import key_health as kh_mod
from app.services import key_rotation as kr_mod
from app.services import queue as queue_mod
from app.services import voice_choice as vc_mod
from app.services import voxcpm_engine as vox_mod
from app.services.i18n import t
from app.services.recap_engine import core as pkg_core
from app.services.recap_engine import exceptions as recap_exc
from app.services.recap_engine import voxcpm_engine as recap_vox_mod
from app.webapp import api as api_mod


def _make_wav(path: Path, hz: float = 160.0, duration_s: float = 2.2, sr: int = 24000) -> Path:
    n = max(1, int(round(duration_s * sr)))
    ts = np.arange(n, dtype=np.float64) / sr
    sig = (0.6 * np.sin(2.0 * np.pi * hz * ts) * 14000.0).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(sig.tobytes())
    return path


@pytest.fixture(autouse=True)
def _isolated_kv_store(monkeypatch):
    """Isolate `bot_settings` sync KV reads/writes in memory for fast deterministic tests."""
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
    vox_mod.VoxCPMEngine._token_rr_counter = 0
    vox_mod.VoxCPMEngine._token_bind_rr_counters.clear()
    recap_vox_mod.VoxCPMEngine._token_cooldowns.clear()
    recap_vox_mod.VoxCPMEngine._token_balances.clear()
    recap_vox_mod.VoxCPMEngine._space_admission_prices.clear()
    recap_vox_mod.VoxCPMEngine._token_rr_counter = 0
    recap_vox_mod.VoxCPMEngine._token_bind_rr_counters.clear()
    queue_mod.reset_fairness_for_tests()
    yield kv
    hf_ledger.reset_ledger_for_tests(clear_store=True)


@pytest_asyncio.fixture
async def db_env(tmp_path, monkeypatch):
    """In-memory async SQLite DB + isolated storage_root wired to AsyncSessionLocal."""
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
    api_mod.RECAP_JOBS_REGISTRY.clear()
    try:
        yield maker, storage
    finally:
        api_mod.RECAP_JOBS_REGISTRY.clear()
        await eng.dispose()


async def _seed_user(
    session: AsyncSession,
    user_id: int,
    *,
    paid: bool,
    voice_seconds: int = 600,
    recap_credits: int = 5,
    lang: str = "my",
) -> User:
    u = User(
        id=user_id,
        username=f"u_{user_id}",
        first_name=f"User{user_id}",
        language_code=lang,
        account_category=AccountCategory.PURCHASED.value if paid else AccountCategory.FREE.value,
        recap_credits=float(recap_credits),
        recap_credit_seconds=int(recap_credits) * 60,
    )
    mb = MinuteBalance(
        user_id=user_id,
        welcome_seconds=voice_seconds if not paid else 0,
        purchased_seconds=voice_seconds if paid else 0,
    )
    session.add(u)
    session.add(mb)
    await session.commit()
    return u


# ═══════════════════════════════════════════════════════════════════════════
# ADDENDUM + PART 4 (#4) — Space List Hygiene & Third-Party Space Removal
# ═══════════════════════════════════════════════════════════════════════════

def test_addendum_1_prod_voxcpm_spaces_excludes_third_party_spaces(monkeypatch):
    monkeypatch.setenv(
        "VOXCPM_SPACES",
        "awints001/voxcpm2-a,awints001/voxcpm2-b,oneclick-org/voxcpm2-a",
    )
    monkeypatch.delenv("VOXCPM_SPACE_URL", raising=False)
    monkeypatch.delenv("VOXCPM_API_URL", raising=False)

    assert vox_mod.DEFAULT_VOXCPM_SPACES == ["openbmb/VoxCPM-Demo"]
    assert recap_vox_mod.DEFAULT_VOXCPM_SPACES == ["openbmb/VoxCPM-Demo"]

    for mod in (vox_mod, recap_vox_mod):
        merged = mod.get_configured_spaces()
        assert "yeyee2609/voxcpm2" not in merged
        assert "soemintheinsmt6/myanmar-voxcpm2" not in merged
        assert merged[:3] == [
            "awints001/voxcpm2-a",
            "awints001/voxcpm2-b",
            "oneclick-org/voxcpm2-a",
        ]
        assert mod._CLONE_INCAPABLE_SPACE_MARKERS == ()


def test_addendum_2_empty_voxcpm_spaces_cpu_demo_tail_never_primary_or_failover(tmp_path, monkeypatch):
    monkeypatch.setenv("VOXCPM_SPACES", "")
    monkeypatch.delenv("VOXCPM_SPACE_URL", raising=False)
    monkeypatch.delenv("VOXCPM_API_URL", raising=False)
    monkeypatch.setenv("HF_TOKENS", "hf_tok_a")

    assert vox_mod.get_configured_spaces() == ["openbmb/VoxCPM-Demo"]
    assert recap_vox_mod.get_configured_spaces() == ["openbmb/VoxCPM-Demo"]

    eng = vox_mod.VoxCPMEngine(work_dir=tmp_path)
    # Primary space guard: CPU demo is never promoted as primary working default
    assert eng.primary_space == ""
    assert vox_mod.is_cpu_demo_space("openbmb/VoxCPM-Demo") is True

    # Automated failover excludes openbmb/VoxCPM-Demo completely (0 client submits!)
    submitted_spaces: list[str] = []
    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: submitted_spaces.append(sp)),
    )
    with pytest.raises(RuntimeError):
        eng.synthesize("မင်္ဂလာပါ။", tmp_path / "out.wav", allow_fallback=False)
    assert submitted_spaces == []


def test_addendum_3_v1_exclusion_pin_holds(monkeypatch):
    monkeypatch.setenv(
        "VOXCPM_SPACES",
        "yeyee2609/voxcpm,https://huggingface.co/spaces/yeyee2609/voxcpm,awints001/voxcpm2-a",
    )
    monkeypatch.setenv("VOXCPM_SPACE_URL", "yeyee2609/voxcpm")
    for mod in (vox_mod, recap_vox_mod):
        merged = mod.get_configured_spaces()
        assert all("yeyee2609/voxcpm" != s.lower().rstrip("/") for s in merged)
        assert not any(mod.is_voxcpm_v1_space(s) for s in merged)
        assert merged[0] == "awints001/voxcpm2-a"


# ═══════════════════════════════════════════════════════════════════════════
# (a) PART 1 — Park Class Identity, End-to-End Recap Park & Segmented Mode
# ═══════════════════════════════════════════════════════════════════════════

def test_a1_recap_park_requested_single_class_identity_in_pipeline_import_order():
    from app.services.recap_engine import recap_pipeline  # triggers sys.path + `import core`

    assert recap_pipeline is not None
    raw_core = sys.modules["core"]
    assert raw_core.RecapParkRequested is pkg_core.RecapParkRequested
    assert raw_core.RecapParkRequested is recap_exc.RecapParkRequested
    assert raw_core.RecapFreeQuotaExhaustedError is pkg_core.RecapFreeQuotaExhaustedError
    assert raw_core.RecapCloneSynthesisError is pkg_core.RecapCloneSynthesisError


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
@pytest.mark.asyncio
async def test_a2_end_to_end_recap_pipeline_quota_at_seg_2_of_3_parks_with_checkpoint_and_buttons(
    db_env, tmp_path, monkeypatch
):
    maker, storage = db_env
    async with maker() as session:
        user = await _seed_user(session, 1001, paid=True, voice_seconds=600, recap_credits=5)
        job_id = "rc_1795520873_park01"
        await billing_mod.reserve_for_job(
            session,
            user_id=user.id,
            job_id=job_id,
            job_type=JobType.RECAP.value,
            billing_mode=billing_mod.BILLING_MODE_CREDIT_PLUS_VOICE,
            reserve_recap_seconds=60,
            reserve_voice_seconds=60,
            note="E2E park test",
        )
        db_job = Job(
            id=job_id,
            user_id=user.id,
            chat_id=user.id,
            status=JobStatus.QUEUED.value,
            job_type=JobType.RECAP.value,
            engine="recap",
            preset_voice="komin",
        )
        session.add(db_job)
        await session.commit()

    # Seed ledger with a deterministic reset timestamp for refill ETA
    now = time.time()
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest("hf_test_1"): {
                    "status": "ok",
                    "remaining_s": 0,
                    "base_s": 300,
                    "resets_at": datetime.fromtimestamp(now + 1800, tz=timezone.utc).isoformat(),
                    "cooldown_until": "",
                    "updated_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                }
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )

    src_mp4 = storage / "temp" / "recap_uploads" / "src3s.mp4"
    src_mp4.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=duration=3:size=320x180:rate=24",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(src_mp4),
        ],
        check=True,
    )

    job_dir = storage / "recap_output" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    plan_data = {
        "segments": [
            {"segment_id": 1, "source_index": 0, "video_start": 0.0, "video_end": 1.0, "narration": "ပထမ စာကြောင်း။"},
            {"segment_id": 2, "source_index": 1, "video_start": 1.0, "video_end": 2.0, "narration": "ဒုတိယ စာကြောင်း။"},
            {"segment_id": 3, "source_index": 2, "video_start": 2.0, "video_end": 3.0, "narration": "တတိယ စာကြောင်း။"},
        ]
    }
    (job_dir / "smart_sync_plan.json").write_text(
        json.dumps(plan_data, ensure_ascii=False), encoding="utf-8"
    )

    class _Seg2QuotaVox:
        def __init__(self, **kw):
            self.pinned_space = "awints001/voxcpm2-a"

        def _adapt_reference_for_clone(self, p):
            return Path(p)

        def synthesize(self, text, output_wav, **kw):
            if "ပထမ" in text:
                return _make_wav(Path(output_wav), duration_s=0.9)
            raise recap_vox_mod.VoxCPMQuotaExhaustedError("ZeroGPU quota exhausted at seg 2")

    monkeypatch.setattr(recap_vox_mod, "VoxCPMEngine", _Seg2QuotaVox)
    monkeypatch.setenv("RECAP_TTS_WORKERS", "1")

    from app.services.downloader import MediaDownloader
    from app.services.recap_engine import recap_planner as rp_mod
    from app.services.recap_engine.recap_pipeline import run_recap_pipeline

    monkeypatch.setattr(
        rp_mod.RecapPlanBuilder,
        "build",
        lambda self_pl, **kw: (job_dir / "smart_sync_plan.json", plan_data),
    )
    if "recap_planner" in sys.modules:
        monkeypatch.setattr(
            sys.modules["recap_planner"].RecapPlanBuilder,
            "build",
            lambda self_pl, **kw: (job_dir / "smart_sync_plan.json", plan_data),
        )

    def _real_pipeline_bridge(self_dl, url_or_path, target_dir, **kw):
        return run_recap_pipeline(
            source_video_path=src_mp4,
            job_dir=target_dir,
            voice=kw.get("voice", "voxcpm_komin"),
            tts_engine="voxcpm",
            paid_tier=kw.get("paid_tier", True),
            voice_choice_mode=kw.get("voice_choice_mode"),
            burn_subtitles=False,
            blur_subtitles=False,
            protection_level="off",
        )

    monkeypatch.setattr(MediaDownloader, "generate_recap_video", _real_pipeline_bridge)

    sent_messages: list[dict] = []

    class _FakeBot:
        def __init__(self):
            self.session = SimpleNamespace(close=AsyncMock())

        async def send_message(self, **kw):
            sent_messages.append(kw)
            return SimpleNamespace(message_id=777)

    monkeypatch.setattr(api_mod, "create_telegram_bot", lambda *a, **kw: _FakeBot())

    api_mod.RECAP_JOBS_REGISTRY[job_id] = {
        "id": job_id,
        "user_id": 1001,
        "title": "Park E2E Test",
        "status": "QUEUED",
        "credit_deducted": True,
        "credits_charged": 1.0,
        "recap_seconds_held": 60,
        "voice_seconds_held": 60,
        "billing_mode": api_mod._RECAP_BILLING_CREDIT_PLUS_VOICE,
        "tts_engine": "voxcpm",
    }
    req = api_mod.RecapConfigRequest(
        url=str(src_mp4),
        title="Park E2E Test",
        voice_slug="komin",
        tts_engine="voxcpm",
    )
    await api_mod._execute_recap_background_job(
        job_id,
        req,
        user_id=1001,
        chat_id=1001,
        is_vip=True,
        credit_deducted=True,
        phase="render",
        resume_source=str(src_mp4),
    )

    async with maker() as session:
        fresh = await session.get(Job, job_id)
        snap = await billing_mod.get_wallet_snapshot(session, 1001)

    assert fresh.status == JobStatus.WAITING_CHOICE.value
    assert api_mod.RECAP_JOBS_REGISTRY[job_id]["status"] == "WAITING_CHOICE"
    # Reservation stays held (0 refunded, 0 settled)
    assert api_mod.RECAP_JOBS_REGISTRY[job_id].get("credit_refunded") is not True
    assert snap["reserved_recap_seconds"] == 60
    assert snap["reserved_voice_seconds"] == 60

    # Segment 1 audio is checkpointed on disk
    ckpt_file = job_dir / "edge_tts_smart_sync" / "checkpoint.json"
    assert ckpt_file.is_file()
    ckpt_json = json.loads(ckpt_file.read_text(encoding="utf-8"))
    assert len(ckpt_json["audio"]) == 1

    vc_state = vc_mod.parse_choice_state(fresh.voice_choice_state)
    assert vc_state["completed_units"] == 1
    assert vc_state["total_units"] == 3
    assert vc_state["pinned_space"] == "awints001/voxcpm2-a"
    assert vc_state["prompt_message_id"] == 777

    # Exact Burmese park card + buttons (`dp:edgemix` absent!)
    assert len(sent_messages) == 1
    dm_text = sent_messages[0]["text"]
    assert "⏸️ Clone အသံကို segment 1/3 မှာ ခဏ ရပ်ထားပါတယ် — GPU quota ပြည့်နေလို့ပါ။" in dm_text
    assert "Credit နဲ့ Voice minute တွေ ဆုံးရှုံးမှု မရှိပါ။" in dm_text
    cb_datas = [
        btn.callback_data
        for row in sent_messages[0]["reply_markup"].inline_keyboard
        for btn in row
    ]
    assert cb_datas == [
        f"dp:wait:{job_id}",
        f"dp:edgeall:{job_id}",
        f"dp:cancel:{job_id}",
    ]
    assert all("edgemix" not in cb for cb in cb_datas)


@pytest.mark.asyncio
async def test_a3_segmented_mode_reraises_recap_park_requested_on_part_2_without_partial_ship(
    db_env, monkeypatch
):
    maker, storage = db_env
    job_id = "rc_segmode_part2_park"
    async with maker() as session:
        user = await _seed_user(session, 1002, paid=True, voice_seconds=900, recap_credits=10)
        await billing_mod.reserve_for_job(
            session,
            user_id=user.id,
            job_id=job_id,
            job_type=JobType.RECAP.value,
            billing_mode=billing_mod.BILLING_MODE_CREDIT_PLUS_VOICE,
            reserve_recap_seconds=180,
            reserve_voice_seconds=180,
        )
        session.add(
            Job(
                id=job_id,
                user_id=user.id,
                chat_id=user.id,
                status=JobStatus.QUEUED.value,
                job_type=JobType.RECAP.value,
                engine="recap",
                preset_voice="komin",
            )
        )
        await session.commit()

    src_vid = storage / "temp" / "recap_uploads" / "long_vid.mp4"
    src_vid.parent.mkdir(parents=True, exist_ok=True)
    src_vid.write_bytes(b"\x00" * 4096)
    seg1_src = storage / "temp" / "seg1.mp4"
    seg2_src = storage / "temp" / "seg2.mp4"
    seg1_src.write_bytes(b"\x00" * 2048)
    seg2_src.write_bytes(b"\x00" * 2048)

    import app.services.downloader as dl_mod
    import app.services.recap_engine.video_splitter as vs_mod

    monkeypatch.setattr(dl_mod, "probe_local_media", lambda _p: {"duration": 720.0, "has_video": True, "has_audio": True})
    monkeypatch.setattr(
        vs_mod,
        "split_source_into_segments",
        lambda *_a, **_kw: [
            {"n": 1, "path": str(seg1_src), "start": 0.0, "end": 360.0},
            {"n": 2, "path": str(seg2_src), "start": 360.0, "end": 720.0},
        ],
    )

    def _fake_gen_recap(self_dl, url_or_path, target_dir, **kw):
        p_idx = kw.get("part_index", 1)
        tdir = Path(target_dir)
        tdir.mkdir(parents=True, exist_ok=True)
        if p_idx == 1:
            out_v = tdir / "part1_done.mp4"
            out_v.write_bytes(b"\x00" * 4096)
            return {"final_video": str(out_v), "segments": 5, "duration": 60.0}
        raise recap_exc.RecapParkRequested(
            "Quota hit on part 2", completed_units=1, total_units=2, pinned_space="awints001/voxcpm2-a"
        )

    monkeypatch.setattr(dl_mod.MediaDownloader, "generate_recap_video", _fake_gen_recap)
    monkeypatch.setattr(
        api_mod,
        "create_telegram_bot",
        lambda *a, **kw: SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=888)),
            session=SimpleNamespace(close=AsyncMock()),
        ),
    )

    api_mod.RECAP_JOBS_REGISTRY[job_id] = {
        "id": job_id,
        "user_id": 1002,
        "title": "Split Recap",
        "status": "QUEUED",
        "split_video": True,
        "credit_deducted": True,
        "credits_charged": 3.0,
        "recap_seconds_held": 180,
        "voice_seconds_held": 180,
        "billing_mode": api_mod._RECAP_BILLING_CREDIT_PLUS_VOICE,
        "tts_engine": "voxcpm",
    }
    req = api_mod.RecapConfigRequest(
        url=str(src_vid),
        title="Split Recap",
        voice_slug="komin",
        tts_engine="voxcpm",
        split_video=True,
        split_minutes=6,
        duration_seconds=720.0,
    )
    await api_mod._execute_recap_background_job(
        job_id,
        req,
        user_id=1002,
        chat_id=1002,
        is_vip=True,
        credit_deducted=True,
        phase="full",
    )

    async with maker() as session:
        fresh = await session.get(Job, job_id)
    # Part 2+ park MUST park the job (never ship a partial 1-part video as SUCCESS!)
    assert fresh.status == JobStatus.WAITING_CHOICE.value
    assert api_mod.RECAP_JOBS_REGISTRY[job_id]["status"] == "WAITING_CHOICE"
    part1_saved = storage / "recap_output" / job_id / "parts" / "recap_part_01.mp4"
    assert part1_saved.is_file()


# ═══════════════════════════════════════════════════════════════════════════
# (b) PART 2 — No-Fallback Invariant, `dp:edgemix` Retired & `dp:cancel`
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
@pytest.mark.parametrize("paid_tier", [False, True])
@pytest.mark.parametrize("err_kind", ["quota", "timeout"])
def test_b1_no_fallback_7_segment_clone_recap_quota_and_timeout(
    tmp_path, monkeypatch, paid_tier, err_kind
):
    job_dir = tmp_path / f"recap_{paid_tier}_{err_kind}"
    job_dir.mkdir(parents=True, exist_ok=True)
    src = job_dir / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=duration=7:size=320x180:rate=24",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(src),
        ],
        check=True,
    )
    plan = {
        "segments": [
            {"segment_id": i + 1, "source_index": i, "video_start": float(i), "video_end": float(i + 1), "narration": f"အပိုင်း {i + 1} စာသား။"}
            for i in range(7)
        ]
    }
    (job_dir / "smart_sync_plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    persona_calls: list = []
    edge_calls: list = []
    import app.services.curated_voices as cv_mod

    monkeypatch.setattr(cv_mod, "synthesize_curated_persona", lambda *a, **kw: persona_calls.append((a, kw)))
    monkeypatch.setattr(pkg_core.EdgeTTSEngine, "generate_channel99_voice", lambda *a, **kw: edge_calls.append((a, kw)))
    monkeypatch.setattr(pkg_core, "_VOX_TRANSIENT_BACKOFF_S", 0.0)

    class _FailingVox:
        def __init__(self, **kw):
            self.pinned_space = "awints001/voxcpm2-a"

        def _adapt_reference_for_clone(self, p):
            return Path(p)

        def synthesize(self, text, output_wav, **kw):
            assert kw.get("allow_fallback") is False
            if "အပိုင်း 4" in text or "အပိုင်း ၄" in text or "လေး" in text:
                if err_kind == "quota":
                    raise recap_vox_mod.VoxCPMQuotaExhaustedError("ZeroGPU quota dead")
                raise TimeoutError("VoxCPM 2 Space queue timed out after 60s")
            return _make_wav(Path(output_wav), duration_s=0.8)

    monkeypatch.setattr(recap_vox_mod, "VoxCPMEngine", _FailingVox)

    sync = pkg_core.EdgeSmartSync(
        job_dir=str(job_dir),
        source_path=str(src),
        plan_path=str(job_dir / "smart_sync_plan.json"),
        voice="voxcpm_komin",
        overlays={"tts_engine": "voxcpm"},
        paid_tier=paid_tier,
        tts_workers=1,
        log=lambda _m: None,
    )
    expected_exc = recap_exc.RecapParkRequested if paid_tier else recap_exc.RecapFreeQuotaExhaustedError
    with pytest.raises(expected_exc):
        sync.run()

    assert persona_calls == []
    assert edge_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("paid_tier", [False, True])
@pytest.mark.parametrize("err_kind", ["quota", "timeout"])
async def test_b2_no_fallback_multi_chunk_standalone_clone_quota_and_timeout(
    db_env, tmp_path, monkeypatch, paid_tier, err_kind
):
    maker, _storage = db_env
    uid = 2000 + (10 if paid_tier else 0) + (1 if err_kind == "timeout" else 0)
    jid = f"vc_{uid}"
    long_text = ("မင်္ဂလာပါ ခင်ဗျာ ဒီနေ့မှာ အသံပွားစနစ်ကို စမ်းသပ်နေပါသည်။ " * 16).strip()

    async with maker() as session:
        await _seed_user(session, uid, paid=paid_tier, voice_seconds=600)
        await billing_mod.reserve_for_job(
            session,
            user_id=uid,
            job_id=jid,
            job_type=JobType.VOICE_CLONE.value,
            billing_mode=billing_mod.BILLING_MODE_VOICE_ONLY,
            reserve_recap_seconds=0,
            reserve_voice_seconds=90,
        )
        job = Job(
            id=jid,
            user_id=uid,
            chat_id=uid,
            status=JobStatus.QUEUED.value,
            job_type=JobType.VOICE_CLONE.value,
            engine="voxcpm",
            preset_voice="komin",
            input_text=long_text,
            seconds_estimated=90,
        )
        session.add(job)
        await session.commit()

    persona_calls: list = []
    edge_calls: list = []
    import app.services.curated_voices as cv_mod

    monkeypatch.setattr(cv_mod, "synthesize_curated_persona", lambda *a, **kw: persona_calls.append(1))
    monkeypatch.setattr(worker_mod, "generate_edge_tts", AsyncMock(side_effect=lambda *a, **kw: edge_calls.append(1)))
    monkeypatch.setattr(vox_mod, "TRANSIENT_RETRY_BACKOFF_SECONDS", 0.0)

    call_idx = {"n": 0}

    def _fake_synth(self_eng, text, output_wav, **kw):
        assert kw.get("allow_fallback") is False
        call_idx["n"] += 1
        if call_idx["n"] >= 2:
            if err_kind == "quota":
                raise vox_mod.VoxCPMQuotaExhaustedError("ZeroGPU quota exhausted")
            raise TimeoutError("VoxCPM 2 Space 'awints001/voxcpm2-a' queue timed out after 60s")
        return _make_wav(Path(output_wav), duration_s=2.0)

    monkeypatch.setattr(vox_mod.VoxCPMEngine, "synthesize", _fake_synth)

    async with maker() as session:
        j = await session.get(Job, jid)
        await worker_mod.process_single_job(session, j, bot=None)
        await session.refresh(j)
        snap = await billing_mod.get_wallet_snapshot(session, uid)

    assert persona_calls == []
    assert edge_calls == []
    if paid_tier:
        assert j.status == JobStatus.WAITING_CHOICE.value
        assert snap["reserved_voice_seconds"] == 90
    else:
        assert j.status == JobStatus.FAILED.value
        assert "မိနစ် လုံးဝ မဖြတ်ထားပါ" in (j.error_message or "")
        assert "10m" in (j.error_message or "")
        assert snap["reserved_voice_seconds"] == 0
        assert snap["available_voice_seconds"] == 600


@pytest.mark.asyncio
async def test_b3_dp_edgemix_retired_and_dp_cancel_refunds_100_percent_and_frees_slot(db_env):
    maker, _storage = db_env
    # Persisted legacy `edge_mix` degrades cleanly to Wait default (`choice_mode` -> None)
    degraded = vc_mod.parse_choice_state(json.dumps({"kind": "recap", "choice": "edge_mix"}))
    assert degraded["choice"] == vc_mod.CHOICE_WAIT
    assert vc_mod.choice_mode(degraded) is None

    async with maker() as session:
        await _seed_user(session, 2099, paid=True, voice_seconds=600)
        jid = "vc_parked_cancel"
        await billing_mod.reserve_for_job(
            session,
            user_id=2099,
            job_id=jid,
            job_type=JobType.VOICE_CLONE.value,
            billing_mode=billing_mod.BILLING_MODE_VOICE_ONLY,
            reserve_recap_seconds=0,
            reserve_voice_seconds=120,
        )
        job = Job(
            id=jid,
            user_id=2099,
            chat_id=2099,
            status=JobStatus.WAITING_CHOICE.value,
            job_type=JobType.VOICE_CLONE.value,
            engine="voxcpm",
            voice_choice_state=vc_mod.dump_choice_state(
                vc_mod.new_choice_state(vc_mod.KIND_CLONE, completed_units=1, total_units=3, needed_gpu_s=144.0)
            ),
        )
        session.add(job)
        await session.commit()

        # `edge_mix` is rejected
        bad = await vc_mod.apply_user_choice(session, job, "edge_mix", 2099)
        assert bad == "bad_choice"

        # `dp:cancel` flips WAITING_CHOICE -> CANCELLED and refunds 100% immediately
        hf_ledger.acquire_in_flight_gpu(jid, 144.0)
        out = await vc_mod.apply_user_choice(session, job, vc_mod.CHOICE_CANCEL, 2099)
        assert out == "ok_cancel"
        await session.refresh(job)
        snap = await billing_mod.get_wallet_snapshot(session, 2099)

    assert job.status == JobStatus.CANCELLED.value
    assert snap["reserved_voice_seconds"] == 0
    assert snap["available_voice_seconds"] == 600
    assert hf_ledger.load_pool_state()["in_flight_gpu_s"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# (c) PART 4 (#1) — `job.cancel()` Called on Engine Poll Timeout Path
# ═══════════════════════════════════════════════════════════════════════════

def test_c_engine_poll_timeout_calls_gradio_job_cancel_before_raising(tmp_path, monkeypatch):
    monkeypatch.setenv("VOXCPM_SPACES", "awints001/voxcpm2-a")
    monkeypatch.setenv("HF_TOKENS", "hf_tok_cancel_1")
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)

    cancel_events: list[str] = []

    class _HungGradioJob:
        def done(self) -> bool:
            return False

        def status(self):
            return SimpleNamespace(eta=45.0, queue_position=2)

        def cancel(self):
            cancel_events.append("cancelled")

    class _FakeClient:
        def submit(self, *a, **kw):
            return _HungGradioJob()

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _FakeClient()),
    )

    eng = vox_mod.VoxCPMEngine(work_dir=tmp_path, spaces=["awints001/voxcpm2-a"], hf_tokens=["hf_tok_cancel_1"])
    with pytest.raises(RuntimeError) as exc_info:
        eng.synthesize(
            "စမ်းသပ် စာကြောင်း။",
            tmp_path / "out.wav",
            timeout=1,
            total_timeout=5,
            allow_fallback=False,
        )
    assert "timed out" in str(exc_info.value).lower()
    assert len(cancel_events) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# (d) PART 6 (#1 & #2) — Best-Fit Token Selection & Pre-Job Gate
# ═══════════════════════════════════════════════════════════════════════════

def test_d1_best_fit_picks_smallest_sufficient_balance_and_skips_sub_gate_tokens(tmp_path, monkeypatch):
    tok_sub = "hf_tok_40s"
    tok_big = "hf_tok_300s"
    tok_fit = "hf_tok_110s"
    tok_mid = "hf_tok_200s"
    now = time.time()

    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_sub): {"status": "ok", "remaining_s": 40, "base_s": 300, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_big): {"status": "ok", "remaining_s": 300, "base_s": 300, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_fit): {"status": "ok", "remaining_s": 110, "base_s": 300, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_mid): {"status": "ok", "remaining_s": 200, "base_s": 300, "resets_at": "", "cooldown_until": "", "updated_at": ""},
            },
            "space_gates": {"awints001/voxcpm2-a": 94},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )

    tried_tokens: list[str | None] = []
    src_wav = _make_wav(tmp_path / "gen.wav", duration_s=1.2)
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)

    class _FakeClient:
        def __init__(self, tok):
            self.tok = tok

        def submit(self, *a, **kw):
            tried_tokens.append(self.tok)
            if len(tried_tokens) == 1:
                raise RuntimeError("transient 502 on first Best-Fit pick")
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(src_wav))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _FakeClient(hf_token)),
    )

    eng = vox_mod.VoxCPMEngine(
        work_dir=tmp_path,
        spaces=["awints001/voxcpm2-a"],
        hf_tokens=[tok_sub, tok_big, tok_fit, tok_mid],
    )
    eng.synthesize("မင်္ဂလာပါ။", tmp_path / "bestfit.wav", skip_mastering=True, allow_fallback=False)

    # Sub-gate 40s token (< 94s gate) was NEVER tried; 110s picked first, 200s second, 300s preserved!
    assert tok_sub not in tried_tokens
    assert tried_tokens == [tok_fit, tok_mid]


def test_d2_pre_job_gate_rejects_unfinishable_job_and_admits_fitting_job():
    now = time.time()
    # 2 tokens: one has 120s (>= 60s gate = 2 slots), one has 18s (< 25s quota dust -> excluded!)
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest("hf_ok"): {"status": "ok", "remaining_s": 120, "base_s": 200, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest("hf_dust"): {"status": "ok", "remaining_s": 18, "base_s": 50, "resets_at": "", "cooldown_until": "", "updated_at": ""},
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )
    metrics = hf_ledger.compute_pool_metrics(now_ts=now)
    # 18s dust excluded -> effective_usable == 120s, solvent_slots == 2
    assert metrics.effective_usable_gpu_s == 120.0
    assert metrics.solvent_slots == 2

    # 1-unit job (needs 1 * 60 * 1.20 = 72s <= 120s, 1 slot <= 2) -> ADMITTED
    ok_dec = hf_ledger.evaluate_voxcpm_admission(kind="clone", is_paid=False, units=1, est_audio_seconds=20.0, now_ts=now)
    assert ok_dec.admitted is True

    # 3-unit job (needs 3 * 60 * 1.20 = 216s > 120s, 3 slots > 2) -> REJECTED
    bad_dec = hf_ledger.evaluate_voxcpm_admission(kind="clone", is_paid=False, units=3, est_audio_seconds=50.0, now_ts=now)
    assert bad_dec.admitted is False
    assert bad_dec.action == "reject_free_capacity"


# ═══════════════════════════════════════════════════════════════════════════
# (e) PART 3 (#4 & #5) — Shared-Ledger Resumer (1.25x) & 24h Safety Valve
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_e_resumer_reads_shared_ledger_and_enforces_1_25x_threshold_and_24h_valve(db_env):
    maker, _storage = db_env
    now = time.time()
    async with maker() as session:
        await _seed_user(session, 3001, paid=True, voice_seconds=600)
        await _seed_user(session, 3002, paid=True, voice_seconds=600)

        await billing_mod.reserve_for_job(
            session, user_id=3001, job_id="vc_res_1", job_type=JobType.VOICE_CLONE.value,
            billing_mode=billing_mod.BILLING_MODE_VOICE_ONLY, reserve_recap_seconds=0, reserve_voice_seconds=100,
        )
        await billing_mod.reserve_for_job(
            session, user_id=3002, job_id="vc_exp_24h", job_type=JobType.VOICE_CLONE.value,
            billing_mode=billing_mod.BILLING_MODE_VOICE_ONLY, reserve_recap_seconds=0, reserve_voice_seconds=100,
        )

        # Job 1 needs 200s GPU -> resumer threshold is 200 * 1.25 = 250s
        st1 = vc_mod.new_choice_state(vc_mod.KIND_CLONE, completed_units=1, total_units=3, needed_gpu_s=200.0)
        # Job 2 parked 25 hours ago (> 24h valve, < old 48h valve!)
        st2 = vc_mod.new_choice_state(vc_mod.KIND_CLONE, completed_units=0, total_units=2, needed_gpu_s=200.0)
        st2["parked_at"] = now - (25 * 3600)

        session.add_all([
            Job(id="vc_res_1", user_id=3001, chat_id=3001, status=JobStatus.WAITING_CHOICE.value, job_type=JobType.VOICE_CLONE.value, engine="voxcpm", voice_choice_state=vc_mod.dump_choice_state(st1)),
            Job(id="vc_exp_24h", user_id=3002, chat_id=3002, status=JobStatus.WAITING_CHOICE.value, job_type=JobType.VOICE_CLONE.value, engine="voxcpm", voice_choice_state=vc_mod.dump_choice_state(st2)),
        ])
        await session.commit()

    # Pool has 240s (< 250s = 200 * 1.25): Job 1 MUST stay parked; Job 2 (>24h) MUST expire & refund!
    hf_ledger.save_pool_state(
        {
            "tokens": {hf_ledger.token_digest("hf_r1"): {"status": "ok", "remaining_s": 240, "base_s": 300, "resets_at": "", "cooldown_until": "", "updated_at": ""}},
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )
    counts1 = await vc_mod.sweep_parked_jobs(bot=None)
    assert counts1 == {"resumed": 0, "expired": 1, "still_parked": 1}

    async with maker() as session:
        j1 = await session.get(Job, "vc_res_1")
        j2 = await session.get(Job, "vc_exp_24h")
        snap2 = await billing_mod.get_wallet_snapshot(session, 3002)
    assert j1.status == JobStatus.WAITING_CHOICE.value
    assert j2.status == JobStatus.FAILED.value
    assert snap2["reserved_voice_seconds"] == 0
    assert snap2["available_voice_seconds"] == 600

    # Simulate worker updating `aj_hf:pool_state` in KV store to 260s (>= 250s = 200 * 1.25)
    hf_ledger.botsetting_sync_write(
        hf_ledger.POOL_STATE_STORE_KEY,
        json.dumps(
            {
                "tokens": {hf_ledger.token_digest("hf_r1"): {"status": "ok", "remaining_s": 260, "base_s": 300, "resets_at": "", "cooldown_until": "", "updated_at": ""}},
                "space_gates": {"awints001/voxcpm2-a": 60},
                "in_flight_gpu_s": 0,
            }
        ),
    )
    counts2 = await vc_mod.sweep_parked_jobs(bot=None)
    assert counts2["resumed"] == 1
    async with maker() as session:
        j1_after = await session.get(Job, "vc_res_1")
    assert j1_after.status == JobStatus.QUEUED.value


# ═══════════════════════════════════════════════════════════════════════════
# (f) PART 3 (#2) — Standalone Chunk-N Resume Reuses Chunks 1..N-1 WAVs
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_f_standalone_clone_chunk_resume_reuses_completed_chunks_without_resynthesis(
    db_env, tmp_path, monkeypatch
):
    maker, storage = db_env
    jid = "vc_chunk_resume_3"
    c_texts = [
        "ပထမ အပိုင်း စာသား ဖြစ်ပါသည်။",
        "ဒုတိယ အပိုင်း စာသား ဖြစ်ပါသည်။",
        "တတိယ အပိုင်း စာသား ဖြစ်ပါသည်။",
    ]
    monkeypatch.setattr(worker_mod, "plan_text_chunks", lambda *a, **kw: list(c_texts))
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: False)
    monkeypatch.setattr(worker_mod, "_implausible_audio_reason", lambda *a, **kw: None)
    monkeypatch.setattr(cp_mod, "apply_rubberband_micro_correction", lambda *_a, **_kw: False)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **_kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )

    ref_src = _make_wav(storage / "temp" / "ref_paid.wav", duration_s=4.0)
    async with maker() as session:
        await _seed_user(session, 4001, paid=True, voice_seconds=600)
        await billing_mod.reserve_for_job(
            session, user_id=4001, job_id=jid, job_type=JobType.VOICE_CLONE.value,
            billing_mode=billing_mod.BILLING_MODE_VOICE_ONLY, reserve_recap_seconds=0, reserve_voice_seconds=60,
        )
        session.add(
            Job(
                id=jid,
                user_id=4001,
                chat_id=4001,
                status=JobStatus.QUEUED.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                input_text=" ".join(c_texts),
                source_audio_path=str(ref_src),
                seconds_estimated=60,
            )
        )
        await session.commit()

    synth_log: list[str] = []
    phase = {"run": 1}

    def _chunk_synth(self_eng, text, output_wav, **kw):
        self_eng.pinned_space = "awints001/voxcpm2-b"
        synth_log.append(text)
        if phase["run"] == 1 and text == c_texts[2]:
            raise vox_mod.VoxCPMQuotaExhaustedError("Quota hit at chunk 3 of 3")
        return _make_wav(Path(output_wav), duration_s=2.0)

    monkeypatch.setattr(vox_mod.VoxCPMEngine, "synthesize", _chunk_synth)

    with patch("app.services.group_ai_admin.transcribe_voice_gemini", new=AsyncMock(return_value="မင်္ဂလာပါ ခင်ဗျာ ဒါက နမူနာ အသံဖိုင် ဖြစ်ပါတယ်။")):
        async with maker() as session:
            job = await session.get(Job, jid)
            res1 = await worker_mod.process_single_job(session, job, bot=None)
            await session.refresh(job)

    assert res1 is False
    assert job.status == JobStatus.WAITING_CHOICE.value
    assert synth_log == [c_texts[0], c_texts[1], c_texts[2]]

    # Chunks 1 & 2 + active_ref.wav + manifest.json preserved in storage/dp_refs/<job_id>/
    ckpt_dir = storage / "dp_refs" / jid
    assert (ckpt_dir / "chunk01.wav").is_file()
    assert (ckpt_dir / "chunk02.wav").is_file()
    assert not (ckpt_dir / "chunk03.wav").is_file()
    assert (ckpt_dir / "active_ref.wav").is_file()
    manifest = json.loads((ckpt_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["completed_units"] == 2
    assert manifest["pinned_space"] == "awints001/voxcpm2-b"

    # Resume run: chunks 1 & 2 MUST NOT be re-synthesized — ONLY chunk 3 is synthesized!
    synth_log.clear()
    phase["run"] = 2
    async with maker() as session:
        job = await session.get(Job, jid)
        job.status = JobStatus.QUEUED.value
        await session.commit()
        res2 = await worker_mod.process_single_job(session, job, bot=None)
        await session.refresh(job)
        snap = await billing_mod.get_wallet_snapshot(session, 4001)

    assert res2 is True
    assert job.status == JobStatus.SUCCESS.value
    assert synth_log == [c_texts[2]]
    assert not ckpt_dir.exists()  # cleaned up after SUCCESS
    assert snap["reserved_voice_seconds"] == 0
    assert snap["available_voice_seconds"] < 600


# ═══════════════════════════════════════════════════════════════════════════
# (g) PART 2 (#4) & PART 3 — Billing Invariance Across Every Terminal Branch
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_g_billing_invariance_zero_net_on_failed_cancelled_expired_and_single_settlement_on_resume(
    db_env, monkeypatch
):
    maker, _storage = db_env
    monkeypatch.setattr(vox_mod, "TRANSIENT_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(worker_mod, "_implausible_audio_reason", lambda *a, **kw: None)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **_kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )

    # 1) Deterministic 5xx error -> FAILED + 100% refund + explicit `(0 ဖြတ်ထားပါသည်)`
    async with maker() as session:
        await _seed_user(session, 5001, paid=True, voice_seconds=500)
        await billing_mod.reserve_for_job(
            session, user_id=5001, job_id="vc_5xx", job_type=JobType.VOICE_CLONE.value,
            billing_mode=billing_mod.BILLING_MODE_VOICE_ONLY, reserve_recap_seconds=0, reserve_voice_seconds=60,
        )
        j_5xx = Job(id="vc_5xx", user_id=5001, chat_id=5001, status=JobStatus.QUEUED.value, job_type=JobType.VOICE_CLONE.value, engine="voxcpm", preset_voice="komin", input_text="မင်္ဂလာပါ။", seconds_estimated=60)
        session.add(j_5xx)
        await session.commit()

        monkeypatch.setattr(vox_mod.VoxCPMEngine, "synthesize", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("VoxCPM upstream HTTP 503")))
        await worker_mod.process_single_job(session, j_5xx, bot=None)
        await session.refresh(j_5xx)
        snap_5xx = await billing_mod.get_wallet_snapshot(session, 5001)

    assert j_5xx.status == JobStatus.FAILED.value
    assert "0 ဖြတ်ထားပါသည်" in (j_5xx.error_message or "")
    assert snap_5xx["available_voice_seconds"] == 500
    assert snap_5xx["reserved_voice_seconds"] == 0

    # 2) Corrupt/missing reference -> FAILED + 100% refund + explicit `(0 ဖြတ်ထားပါသည်)`
    async with maker() as session:
        await _seed_user(session, 5002, paid=False, voice_seconds=300)
        await billing_mod.reserve_for_job(
            session, user_id=5002, job_id="vc_badref", job_type=JobType.VOICE_CLONE.value,
            billing_mode=billing_mod.BILLING_MODE_VOICE_ONLY, reserve_recap_seconds=0, reserve_voice_seconds=45,
        )
        j_ref = Job(id="vc_badref", user_id=5002, chat_id=5002, status=JobStatus.QUEUED.value, job_type=JobType.VOICE_CLONE.value, engine="voxcpm", source_audio_path="/nonexistent/ref.wav", input_text="မင်္ဂလာပါ။", seconds_estimated=45)
        session.add(j_ref)
        await session.commit()
        await worker_mod.process_single_job(session, j_ref, bot=None)
        await session.refresh(j_ref)
        snap_ref = await billing_mod.get_wallet_snapshot(session, 5002)

    assert j_ref.status == JobStatus.FAILED.value
    assert "0 ဖြတ်ထားပါသည်" in (j_ref.error_message or "")
    assert snap_ref["available_voice_seconds"] == 300
    assert snap_ref["reserved_voice_seconds"] == 0

    # 3) Park -> Resume -> Complete settles ONCE (exact actual duration, zero leftover hold)
    attempts = {"n": 0}

    def _park_then_ok(self_eng, text, output_wav, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise vox_mod.VoxCPMQuotaExhaustedError("pool full")
        return _make_wav(Path(output_wav), duration_s=5.0)

    monkeypatch.setattr(vox_mod.VoxCPMEngine, "synthesize", _park_then_ok)
    async with maker() as session:
        await _seed_user(session, 5003, paid=True, voice_seconds=500)
        await billing_mod.reserve_for_job(
            session, user_id=5003, job_id="vc_once", job_type=JobType.VOICE_CLONE.value,
            billing_mode=billing_mod.BILLING_MODE_VOICE_ONLY, reserve_recap_seconds=0, reserve_voice_seconds=60,
        )
        j_once = Job(id="vc_once", user_id=5003, chat_id=5003, status=JobStatus.QUEUED.value, job_type=JobType.VOICE_CLONE.value, engine="voxcpm", preset_voice="komin", input_text="မင်္ဂလာပါ။", seconds_estimated=60)
        session.add(j_once)
        await session.commit()

        await worker_mod.process_single_job(session, j_once, bot=None)
        await session.refresh(j_once)
        assert j_once.status == JobStatus.WAITING_CHOICE.value
        snap_mid = await billing_mod.get_wallet_snapshot(session, 5003)
        assert snap_mid["reserved_voice_seconds"] == 60
        assert snap_mid["available_voice_seconds"] == 440

        j_once.status = JobStatus.QUEUED.value
        await session.commit()
        ok = await worker_mod.process_single_job(session, j_once, bot=None)
        assert ok is True
        await session.refresh(j_once)
        snap_end = await billing_mod.get_wallet_snapshot(session, 5003)

    assert j_once.status == JobStatus.SUCCESS.value
    assert snap_end["reserved_voice_seconds"] == 0
    assert snap_end["available_voice_seconds"] == 500 - j_once.seconds_consumed
    assert 4 <= j_once.seconds_consumed <= 6


# ═══════════════════════════════════════════════════════════════════════════
# (h) PART 7 — Watermark Conservation Modes (>40%, 40->20%, 20->8%, <8%)
# ═══════════════════════════════════════════════════════════════════════════

def test_h1_watermark_bands_and_conservation_disables_prosody_and_clip_regens_while_keeping_rubberband(
    tmp_path, monkeypatch
):
    assert hf_ledger.watermark_band_for_fraction(0.55) == hf_ledger.BAND_NORMAL
    assert hf_ledger.watermark_band_for_fraction(0.30) == hf_ledger.BAND_CONSERVATION
    assert hf_ledger.watermark_band_for_fraction(0.15) == hf_ledger.BAND_SHORT_JOB
    assert hf_ledger.watermark_band_for_fraction(0.05) == hf_ledger.BAND_DRY

    # Put pool at 30% (Conservation band: 40% -> 20%)
    now = time.time()
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest("hf_c1"): {"status": "ok", "remaining_s": 300, "base_s": 1000, "resets_at": "", "cooldown_until": "", "updated_at": ""}
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )
    m = hf_ledger.compute_pool_metrics(now_ts=now)
    assert m.band == hf_ledger.BAND_CONSERVATION
    assert m.allow_optional_gpu_regen is False
    assert m.high_demand_hint is True
    assert (
        t("recap_clone_high_demand_hint", "my")
        == "Clone အသံ လိုအပ်ချက် များနေပါတယ် — Gemini TTS ဆိုရင် ချက်ချင်း ရပါတယ်။"
    )

    ref_wav = _make_wav(tmp_path / "ref150.wav", hz=150.0, duration_s=8.0)
    chunks = ["ပထမ စာကြောင်း။", "ဒုတိယ စာကြောင်း။"]
    monkeypatch.setattr(worker_mod, "plan_text_chunks", lambda *a, **kw: list(chunks))
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: True)  # would regen in Normal!
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **_kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )

    synth_calls: list[str] = []
    rb_calls: list[tuple[float, float]] = []

    def _outlier_synth(**kw):
        synth_calls.append(kw["text"])
        # +11 st outlier (~283 Hz vs 150 Hz ref) — would trigger prosody retry in Normal!
        hz = 150.0 * (2.0 ** (11.0 / 12.0)) if kw["text"] == chunks[1] else 150.0
        return _make_wav(Path(kw["output_wav"]), hz=hz, duration_s=2.2)

    monkeypatch.setattr(
        cp_mod,
        "apply_rubberband_micro_correction",
        lambda _p, *, pitch_shift_st, tempo_factor, ffmpeg_bin=None: (rb_calls.append((pitch_shift_st, tempo_factor)) or True),
    )

    with patch("app.services.group_ai_admin.transcribe_voice_gemini", new=AsyncMock(return_value=None)):
        worker_mod._synthesize_voxcpm_voice_job(
            SimpleNamespace(synthesize=_outlier_synth),
            job_id="vc_cons",
            text=" ".join(chunks),
            output_wav=tmp_path / "out_cons.wav",
            reference_wav=ref_wav,
            control_instruction="",
            preset_voice=None,
            speed="+0%",
            progress_cb=None,
            gender_preset="m2m",
        )

    # Zero edge-clip GPU re-gens and zero prosody-outlier GPU retries (exact 2 calls for 2 chunks),
    # while local CPU rubberband micro-correction STILL ran!
    assert synth_calls == chunks
    assert len(rb_calls) == 2
    assert any(st != 0.0 for st, _tp in rb_calls)


@pytest.mark.asyncio
async def test_h2_watermark_15pct_holds_free_12seg_recap_admits_free_45s_clone_and_5pct_parks_paid_recap(
    db_env, monkeypatch
):
    maker, storage = db_env
    now = time.time()

    # 15% pool watermark (`short_job_priority`: 450s remaining out of 3000s base, gate=60s -> 7 slots)
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest("hf_w1"): {"status": "ok", "remaining_s": 250, "base_s": 1500, "resets_at": datetime.fromtimestamp(now + 1200, tz=timezone.utc).isoformat(), "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest("hf_w2"): {"status": "ok", "remaining_s": 200, "base_s": 1500, "resets_at": datetime.fromtimestamp(now + 1200, tz=timezone.utc).isoformat(), "cooldown_until": "", "updated_at": ""},
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )
    assert hf_ledger.compute_pool_metrics(now_ts=now).band == hf_ledger.BAND_SHORT_JOB

    # Free 12-segment recap at 15% -> HELD (Gemini nudge, 0 deducted, NO failed Job row)
    dec_free_recap = hf_ledger.evaluate_voxcpm_admission(
        kind="recap", is_paid=False, units=12, est_audio_seconds=144.0, now_ts=now
    )
    assert dec_free_recap.admitted is False
    assert dec_free_recap.action == "nudge_gemini"

    # Free 45 s standalone clone at 15% -> ADMITTED and QUEUED!
    async with maker() as session:
        await _seed_user(session, 6001, paid=False, voice_seconds=300, recap_credits=5)
        await _seed_user(session, 6002, paid=True, voice_seconds=600, recap_credits=5)
        short_45s_text = "မင်္ဂလာပါ ခင်ဗျာ ဒီနေ့ သတင်းထူးများကို တင်ဆက်ပေးသွားမှာ ဖြစ်ပါတယ်။ " * 8  # ~2 chunks, ~35-45s
        clone_job, clone_status = await queue_mod.create_voice_clone_job(
            session,
            user_id=6001,
            text=short_45s_text,
            preset_voice="komin",
            engine="voxcpm",
        )
        await session.commit()
    assert clone_job is not None
    assert clone_status == JobStatus.QUEUED.value

    # Now verify endpoint-level Free recap rejection (0 deducted, no Job row) vs 5% Paid recap PARK!
    src_local = storage / "temp" / "recap_uploads" / "clip144.mp4"
    src_local.parent.mkdir(parents=True, exist_ok=True)
    src_local.write_bytes(b"\x00" * 4096)
    import app.services.downloader as dl_mod

    monkeypatch.setattr(dl_mod, "probe_local_media", lambda _p: {"duration": 120.0, "has_video": True, "has_audio": True})
    monkeypatch.setattr(api_mod, "_recap_frame_owner_id", AsyncMock(side_effect=lambda _db, _p: 6001))

    async with maker() as session:
        free_user = await session.get(User, 6001)
        req_free = api_mod.RecapConfigRequest(
            url=str(src_local), title="Free MultiSeg", voice_slug="komin", tts_engine="voxcpm", duration_seconds=120.0
        )
        resp_free = await api_mod.create_recap_job(req_free, user=free_user, db=session, _gate=free_user)
        snap_free = await billing_mod.get_wallet_snapshot(session, 6001)

    assert resp_free.status_code == 503
    body_free = json.loads(resp_free.body.decode("utf-8"))
    assert body_free["code"] == "VOXCPM_GEMINI_SWITCH_REQUIRED"
    assert snap_free["reserved_recap_seconds"] == 0

    # Drop pool to 5% (`dry` gate: 150s remaining out of 3000s base) -> Paid recap PARKS (`WAITING_CHOICE`), NOT fails!
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest("hf_w1"): {"status": "ok", "remaining_s": 150, "base_s": 3000, "resets_at": datetime.fromtimestamp(now + 900, tz=timezone.utc).isoformat(), "cooldown_until": "", "updated_at": ""}
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )
    assert hf_ledger.compute_pool_metrics(now_ts=now).band == hf_ledger.BAND_DRY
    monkeypatch.setattr(api_mod, "_recap_frame_owner_id", AsyncMock(side_effect=lambda _db, _p: 6002))
    monkeypatch.setattr(
        api_mod,
        "create_telegram_bot",
        lambda *a, **kw: SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=901)),
            session=SimpleNamespace(close=AsyncMock()),
        ),
    )

    async with maker() as session:
        paid_user = await session.get(User, 6002)
        req_paid = api_mod.RecapConfigRequest(
            url=str(src_local), title="Paid 5pct Park", voice_slug="komin", tts_engine="voxcpm", duration_seconds=120.0
        )
        resp_paid = await api_mod.create_recap_job(req_paid, user=paid_user, db=session, _gate=paid_user)
        db_paid_job = await session.get(Job, resp_paid["recap_id"])

    assert resp_paid["status"] == "WAITING_CHOICE"
    assert db_paid_job.status == JobStatus.WAITING_CHOICE.value


# ═══════════════════════════════════════════════════════════════════════════
# (i) PART 5 (#3) & PART 8 — Auth-Dead 24h Quarantine & Token Probe CLI
# ═══════════════════════════════════════════════════════════════════════════

def test_i1_auth_dead_401_probed_once_quarantined_24h_excluded_surfaced_and_autocleared(
    tmp_path, monkeypatch, _isolated_kv_store
):
    bad_tok = "hf_secret_revoked_token_DO_NOT_LEAK"
    good_tok = "hf_secret_valid_token_DO_NOT_LEAK"
    whoami_calls: list[str] = []

    monkeypatch.setattr(
        hf_ledger,
        "probe_whoami_status",
        lambda tok, timeout=8.0: (whoami_calls.append(tok), (401, None))[1],
    )
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    gen_wav = _make_wav(tmp_path / "ok_auth.wav", duration_s=1.2)

    tried_in_walk: list[str | None] = []

    class _Client:
        def __init__(self, tok):
            self.tok = tok

        def submit(self, *a, **kw):
            tried_in_walk.append(self.tok)
            if self.tok == bad_tok:
                raise RuntimeError("401 Unauthorized: invalid credentials")
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _Client(hf_token)),
    )

    eng = vox_mod.VoxCPMEngine(
        work_dir=tmp_path,
        spaces=["awints001/voxcpm2-a"],
        hf_tokens=[bad_tok, good_tok],
    )
    eng.synthesize("ပထမ အကြိမ်။", tmp_path / "r1.wav", skip_mastering=True, allow_fallback=False)

    # Probed ONCE via whoami-v2 and quarantined 24h as `auth_dead`
    assert whoami_calls == [bad_tok]
    assert hf_ledger.is_token_auth_dead(bad_tok) is True
    raw_json = _isolated_kv_store[hf_ledger.POOL_STATE_STORE_KEY]
    assert bad_tok not in raw_json
    assert good_tok not in raw_json
    parsed = json.loads(raw_json)
    assert set(parsed.keys()) == {"tokens", "space_gates", "in_flight_gpu_s"}
    assert parsed["tokens"][hf_ledger.token_digest(bad_tok)]["status"] == "auth_dead"

    # Second synthesis walk completely excludes `bad_tok`!
    tried_in_walk.clear()
    eng.synthesize("ဒုတိယ အကြိမ်။", tmp_path / "r2.wav", skip_mastering=True, allow_fallback=False)
    assert bad_tok not in tried_in_walk
    assert tried_in_walk == [good_tok]
    assert len(whoami_calls) == 1  # NOT re-probed on walk!

    # Surfaced in Admin Key Health & auto-cleared when later probe returns 200
    monkeypatch.setattr(kh_mod, "_load_pools", lambda: ([bad_tok, good_tok], [], [], []))
    monkeypatch.setattr(
        kh_mod,
        "_probe_hf_account",
        lambda idx, tok: (
            {"index": idx, "status": "dead", "account": "revoked_u"}
            if tok == bad_tok
            else {"index": idx, "status": "ok", "account": "good_u", "remaining_s": 300.0, "base_s": 300.0}
        ),
    )
    report = kh_mod.run_key_health_check()
    assert report["providers"]["hf"]["auth_dead"] == 1
    assert report["providers"]["hf"]["accounts"][0]["quarantined"] is True

    # Auto-clear when probe returns 200
    hf_ledger.reconcile_from_probe_rows(
        [bad_tok, good_tok],
        [
            {"index": 0, "status": "ok", "remaining_s": 300.0, "base_s": 300.0},
            {"index": 1, "status": "ok", "remaining_s": 300.0, "base_s": 300.0},
        ],
    )
    assert hf_ledger.is_token_auth_dead(bad_tok) is False


def test_i2_probe_hf_tokens_cli_deduplicates_accounts_hides_raw_tokens_and_exits_nonzero_on_invalid(
    tmp_path, capsys
):
    from scripts import probe_hf_tokens as probe_cli

    secret_1 = "hf_SUPER_SECRET_DUP_1"
    secret_2 = "hf_SUPER_SECRET_DUP_2"
    secret_bad = "hf_SUPER_SECRET_BAD_3"
    tok_file = tmp_path / "candidates.txt"
    tok_file.write_text(f"{secret_1}\n{secret_2}\n{secret_bad}\n", encoding="utf-8")

    def _fake_probe(slot: int, tok: str) -> dict:
        if tok in (secret_1, secret_2):
            return {"slot": slot, "status": "ok", "account": "alice_hf", "base_s": 300.0, "remaining_s": 240.0, "resets_at": None, "quota_error": None}
        return {"slot": slot, "status": "auth_dead", "account": None, "base_s": None, "remaining_s": None, "resets_at": None, "quota_error": None}

    rc_bad = probe_cli.main([str(tok_file)], probe_fn=_fake_probe)
    captured_bad = capsys.readouterr()
    assert rc_bad != 0
    assert secret_1 not in captured_bad.out and secret_1 not in captured_bad.err
    assert secret_2 not in captured_bad.out and secret_2 not in captured_bad.err
    assert secret_bad not in captured_bad.out and secret_bad not in captured_bad.err
    assert "Unique HF accounts     : 1" in captured_bad.out
    assert "Duplicate acct tokens  : 1" in captured_bad.out
    assert "alice_hf" in captured_bad.out

    valid_file = tmp_path / "valid_only.txt"
    valid_file.write_text(f"{secret_1}\n{secret_2}\n", encoding="utf-8")
    rc_ok = probe_cli.main([str(valid_file)], probe_fn=_fake_probe)
    captured_ok = capsys.readouterr()
    assert rc_ok == 0
    assert "Total remaining quota  : 240.0 s" in captured_ok.out  # counted ONCE, not 480s!


# ═══════════════════════════════════════════════════════════════════════════
# (j) PART 4 (#2 & #3) & PART 6 (#4) — Recap Workers=2 + Shared Pin + Own-Voice ASR + Queue Fairness
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_j1_recap_voxcpm_workers_capped_at_2_and_share_single_pinned_space(tmp_path, monkeypatch):
    job_dir = tmp_path / "recap_w2"
    job_dir.mkdir(parents=True, exist_ok=True)
    src = job_dir / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=duration=6:size=320x180:rate=24",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(src),
        ],
        check=True,
    )
    plan = {
        "segments": [
            {"segment_id": i + 1, "source_index": i, "video_start": float(i), "video_end": float(i + 1), "narration": f"အပိုင်း {i + 1}။"}
            for i in range(6)
        ]
    }
    (job_dir / "smart_sync_plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    lock = threading.Lock()
    active = {"cur": 0, "peak": 0}
    seen_pins: list[Any] = []

    class _PinTrackingVox:
        def __init__(self, **kw):
            self.space_pin = kw.get("space_pin")
            with lock:
                seen_pins.append(self.space_pin)

        def _adapt_reference_for_clone(self, p):
            return Path(p)

        def synthesize(self, text, output_wav, **kw):
            with lock:
                active["cur"] += 1
                active["peak"] = max(active["peak"], active["cur"])
                if self.space_pin is not None:
                    self.space_pin.pin_if_unset("awints001/voxcpm2-a")
            try:
                time.sleep(0.08)
                return _make_wav(Path(output_wav), duration_s=0.8)
            finally:
                with lock:
                    active["cur"] -= 1

    monkeypatch.setattr(recap_vox_mod, "VoxCPMEngine", _PinTrackingVox)
    monkeypatch.delenv("RECAP_TTS_WORKERS", raising=False)

    sync = pkg_core.EdgeSmartSync(
        job_dir=str(job_dir),
        source_path=str(src),
        plan_path=str(job_dir / "smart_sync_plan.json"),
        voice="voxcpm_komin",
        overlays={"tts_engine": "voxcpm"},
        render_final_video=False,
        log=lambda _m: None,
    )
    assert sync.tts_workers == 6  # Edge/Gemini keep 6
    sync.run()

    # VoxCPM stage capped at 2 workers, and ALL worker slots share the SAME JobSpacePin instance!
    assert active["peak"] <= 2
    non_null_pins = [p for p in seen_pins if p is not None]
    assert len(non_null_pins) >= 1
    assert all(p is non_null_pins[0] for p in non_null_pins)
    assert non_null_pins[0].pinned_space == "awints001/voxcpm2-a"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_j2_own_voice_recap_asr_prepass_invoked_and_skipped_on_gender_flip(tmp_path, monkeypatch):
    own_ref = _make_wav(tmp_path / "own_ref.wav", hz=155.0, duration_s=10.0)
    src = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=duration=2:size=320x180:rate=24",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(src),
        ],
        check=True,
    )
    plan_file = tmp_path / "smart_sync_plan.json"
    plan_file.write_text(
        json.dumps({"segments": [{"segment_id": 1, "source_index": 0, "video_start": 0.0, "video_end": 2.0, "narration": "စမ်းသပ် စာသား။"}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    seen_prompt_texts: list[str] = []

    class _SpyVox:
        def __init__(self, **kw):
            self.space_pin = kw.get("space_pin")

        def _adapt_reference_for_clone(self, p):
            return Path(p)

        def synthesize(self, text, output_wav, **kw):
            seen_prompt_texts.append(str(kw.get("prompt_text") or ""))
            return _make_wav(Path(output_wav), duration_s=1.0)

    monkeypatch.setattr(recap_vox_mod, "VoxCPMEngine", _SpyVox)
    asr_calls: list[Path] = []
    plausible_burmese = "မင်္ဂလာပါ ခင်ဗျာ ဒါက ကိုယ်ပိုင်အသံ နမူနာ စာကြောင်း ဖြစ်ပါတယ်။"
    monkeypatch.setattr(
        cp_mod,
        "transcribe_reference_slice_checked",
        lambda p, dur: (asr_calls.append(Path(p)), plausible_burmese)[1],
    )

    # Case 1: Same-gender own-voice recap -> ASR pre-pass invoked & prompt_text passed to synthesize!
    sync1 = pkg_core.EdgeSmartSync(
        job_dir=str(tmp_path / "job_same"),
        source_path=str(src),
        plan_path=str(plan_file),
        voice="voxcpm_komin",
        overlays={"tts_engine": "voxcpm", "voxcpm_ref_audio": str(own_ref)},
        render_final_video=False,
        tts_workers=1,
        log=lambda _m: None,
    )
    sync1.run()
    assert len(asr_calls) == 1
    assert seen_prompt_texts == [plausible_burmese]

    # Case 2: Explicit m2f gender flip -> ASR pre-pass SKIPPED, prompt_text NOT forced!
    asr_calls.clear()
    seen_prompt_texts.clear()
    sync2 = pkg_core.EdgeSmartSync(
        job_dir=str(tmp_path / "job_flip"),
        source_path=str(src),
        plan_path=str(plan_file),
        voice="voxcpm_mahnin",
        overlays={"tts_engine": "voxcpm", "voxcpm_ref_audio": str(own_ref), "gender_preset": "m2f"},
        render_final_video=False,
        tts_workers=1,
        log=lambda _m: None,
    )
    sync2.run()
    assert asr_calls == []
    assert seen_prompt_texts == [""]


@pytest.mark.asyncio
async def test_j3_global_concurrency_2_fairness_2_to_1_antistarvation_and_worker_affinity(
    db_env, monkeypatch
):
    maker, _storage = db_env
    assert queue_mod.MAX_CONCURRENT_VOXCPM_JOBS == 2
    monkeypatch.setenv("VPS_PUBLIC_HOST", "worker-host-A")

    now_dt = datetime.now(timezone.utc)
    async with maker() as session:
        await _seed_user(session, 7001, paid=True)
        await _seed_user(session, 7002, paid=True)
        await _seed_user(session, 7003, paid=True)
        await _seed_user(session, 7004, paid=False)
        await _seed_user(session, 7005, paid=False)

        # 2 RUNNING VoxCPM jobs -> 3rd VoxCPM claim blocked by MAX_CONCURRENT_VOXCPM_JOBS == 2!
        session.add_all([
            Job(id="run_1", user_id=7001, status=JobStatus.RUNNING.value, job_type=JobType.VOICE_CLONE.value, engine="voxcpm"),
            Job(id="run_2", user_id=7002, status=JobStatus.RUNNING.value, job_type=JobType.VOICE_CLONE.value, engine="voxcpm"),
            Job(id="q_blocked", user_id=7003, status=JobStatus.QUEUED.value, job_type=JobType.VOICE_CLONE.value, engine="voxcpm", input_text="တိုတို။", created_at=now_dt),
        ])
        await session.commit()
        blocked = await queue_mod.claim_next_queued_job(session, engine_filter="voxcpm")
        assert blocked is None

        # Finish the 2 running jobs, then test:
        # - Host affinity block (<15 min on worker-host-B skipped by worker-host-A)
        # - 15-min anti-starvation ceiling (free job waiting 16 min beats fresh paid jobs!)
        # - 2:1 Paid/Free interleaving + shortest-GPU-first within tier
        for r_id in ("run_1", "run_2", "q_blocked"):
            row = await session.get(Job, r_id)
            row.status = JobStatus.SUCCESS.value

        other_host_state = vc_mod.new_choice_state(
            vc_mod.KIND_CLONE, completed_units=1, total_units=2, needed_gpu_s=30.0, worker_host="worker-host-B"
        )
        other_host_state["resumed_at"] = time.time()

        session.add_all([
            Job(id="q_affinity_B", user_id=7001, status=JobStatus.QUEUED.value, job_type=JobType.VOICE_CLONE.value, engine="voxcpm", input_text="တို။", voice_choice_state=vc_mod.dump_choice_state(other_host_state), created_at=now_dt),
            Job(id="q_paid_long", user_id=7001, status=JobStatus.QUEUED.value, job_type=JobType.VOICE_CLONE.value, engine="voxcpm", input_text="ရှည်လျားသော စာကြောင်း။ " * 30, created_at=now_dt - timedelta(seconds=30)),
            Job(id="q_paid_short1", user_id=7002, status=JobStatus.QUEUED.value, job_type=JobType.VOICE_CLONE.value, engine="voxcpm", input_text="တို၁။", created_at=now_dt - timedelta(seconds=20)),
            Job(id="q_paid_short2", user_id=7003, status=JobStatus.QUEUED.value, job_type=JobType.VOICE_CLONE.value, engine="voxcpm", input_text="တို၂။", created_at=now_dt - timedelta(seconds=10)),
            Job(id="q_free_norm", user_id=7004, status=JobStatus.QUEUED.value, job_type=JobType.VOICE_CLONE.value, engine="voxcpm", input_text="တိုအခမဲ့။", created_at=now_dt - timedelta(seconds=25)),
            Job(id="q_free_starved", user_id=7005, status=JobStatus.QUEUED.value, job_type=JobType.VOICE_CLONE.value, engine="voxcpm", input_text="စောင့်နေတာ ကြာပြီ။", created_at=now_dt - timedelta(minutes=16)),
        ])
        await session.commit()

        claimed_order: list[str] = []
        queue_mod.reset_fairness_for_tests()
        for _ in range(5):
            c = await queue_mod.claim_next_queued_job(session, engine_filter="voxcpm")
            assert c is not None
            claimed_order.append(str(c.id))
            c.status = JobStatus.SUCCESS.value  # free slot so we observe pure dispatch order
            await session.commit()

        # 1st: anti-starved free job (>15m), then 2 shortest Paid jobs, then 1 Free job (2:1 interleaving), then longer Paid job!
        # And `q_affinity_B` was skipped the entire time because `worker-host-B` holds its <15m checkpoint!
        assert claimed_order == [
            "q_free_starved",
            "q_paid_short1",
            "q_paid_short2",
            "q_free_norm",
            "q_paid_long",
        ]
        still_b = await session.get(Job, "q_affinity_B")
        assert still_b.status == JobStatus.QUEUED.value
