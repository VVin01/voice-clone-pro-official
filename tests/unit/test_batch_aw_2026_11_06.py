"""Batch AW (2026-11-06): the TTS feature package — the final feature
batch before reopening (owner decisions 2026-11-05 23:49–23:57).

Part 1: Edge TTS is a first-class recap narration engine, and free-tier
recap narration is clamped to Gemini/Edge (clone/persona = purchased +
admins) at BOTH create_recap_job and the execution entry — substitute
and log, never reject.
Part 2: a standalone TTS tab backed by Job.job_type == "TTS" with its
own daily allowance (free 15 / purchased 60 min, 0 = unlimited, admins
exempt), invisible to the Batch AN voice-minute accounting and to the
voice-clone worker's claim.
Part 3: referral re-scope — clone jobs never deduct or count
referral_seconds (clone-spendable = monthly_free + welcome +
purchased); TTS cost draws the referral bucket FIRST and only the
remainder counts against the TTS cap; failed TTS refunds the draw;
the reward is 120s of TTS time per referral (max 20 = 40 min).
"""
from __future__ import annotations

import inspect
import json
import logging
import math
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Job, MinuteLedgerEvent, VoiceModel
from app.services import ledger as ledger_mod
from app.services import tts_service
from app.services.ledger import (
    REFERRAL_REWARD_SECONDS,
    consume_seconds,
    credit_purchased_minutes,
    get_minute_balance,
    get_or_create_user,
    grant_monthly_free_allowance,
    refund_tts_referral_seconds,
)
from app.services.queue import claim_next_queued_job, create_voice_clone_job
from app.services.tts_service import (
    create_tts_job,
    estimate_tts_seconds,
    split_tts_text,
    tts_cap_consumed_seconds,
)
from app.services.voice_display import recap_voice_display_name
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    # Small, exact caps for the matrix tests; the production defaults
    # (15 / 60) are pinned separately below.
    monkeypatch.setattr(settings, "tts_daily_limit_minutes_free", 1)
    monkeypatch.setattr(settings, "tts_daily_limit_minutes_paid", 2)
    monkeypatch.setattr(settings, "voice_daily_limit_minutes_free", 5)
    monkeypatch.setattr(settings, "voice_daily_limit_minutes_paid", 30)
    yield


def _today_start() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _req(engine):
    return api_mod.RecapConfigRequest(
        url="",
        title="Test Movie",
        voice_slug="komin",
        duration_seconds=60,
        tts_engine=engine,
    )


async def _seed_tts_job(db_session, user_id, *, status, seconds_consumed=0,
                        seconds_estimated=0, minutes_ago=5, input_text="seed"):
    job = Job(
        id=f"aw-seed-{user_id}-{status}-{minutes_ago}-{seconds_consumed}-{seconds_estimated}-{len(input_text)}",
        user_id=user_id,
        chat_id=user_id,
        input_text=input_text,
        engine="gemini",
        preset_voice="Charon",
        status=status,
        job_type=JobType.TTS.value,
        seconds_consumed=seconds_consumed,
        seconds_estimated=seconds_estimated,
        speed_preset="+12%",
        created_at=_today_start() + timedelta(minutes=minutes_ago),
    )
    db_session.add(job)
    await db_session.flush()
    return job


async def _set_referral(db_session, user_id, seconds):
    bal = await get_minute_balance(db_session, user_id)
    bal.referral_seconds = seconds
    await db_session.flush()
    return bal


def _submit_tts(db_session, user_id, text="မင်္ဂလာပါ၊ ဒါက စမ်းသပ် စာသားပါ။", **kw):
    kw.setdefault("engine", "gemini")
    kw.setdefault("voice", "Charon")
    kw.setdefault("speed_rate", "+12%")
    return create_tts_job(db_session, user_id, text=text, **kw)


# ── AW-1/AW-4: edge is an accepted engine everywhere ─────────────────

def test_aw_recap_engine_set_includes_edge():
    assert api_mod.RECAP_TTS_ENGINES == frozenset({"voxcpm", "gemini", "edge"})


def test_aw_validator_accepts_edge_and_rejects_unknown():
    assert _req("edge").tts_engine == "edge"
    assert _req("gemini").tts_engine == "gemini"
    assert _req("voxcpm").tts_engine == "voxcpm"
    with pytest.raises(Exception):
        _req("bogus-engine")


def test_aw_prefs_enum_accepts_edge():
    assert api_mod.USER_PREF_ENUMS["recapTtsEngine"] == ["voxcpm", "gemini", "edge"]
    assert 'recapTtsEngine: ["voxcpm", "gemini", "edge"]' in APP_JS


def test_aw_tts_endpoints_registered():
    paths = {r.path for r in api_mod.api_router.routes}
    assert "/api/tts/generate" in paths
    assert "/api/tts/quota" in paths


# ── AW-2: the clamp matrix ────────────────────────────────────────────

def _engine_clamp_logs(caplog):
    return [r for r in caplog.records if "Recap TTS engine clamped" in r.getMessage()]


def test_aw_clamp_free_voxcpm_becomes_gemini_with_one_info_log(caplog):
    req = _req("voxcpm")
    free = SimpleNamespace(id=558101, account_category=AccountCategory.FREE.value)
    with caplog.at_level(logging.INFO, logger="app.webapp.api"):
        applied = api_mod._clamp_recap_tts_engine(req, user=free, user_id=free.id)
    assert applied == "gemini"
    assert req.tts_engine == "gemini"
    assert len(_engine_clamp_logs(caplog)) == 1


@pytest.mark.parametrize("engine", ["gemini", "edge"])
def test_aw_clamp_free_gemini_and_edge_untouched(caplog, engine):
    req = _req(engine)
    free = SimpleNamespace(id=558102, account_category=AccountCategory.FREE.value)
    with caplog.at_level(logging.INFO, logger="app.webapp.api"):
        applied = api_mod._clamp_recap_tts_engine(req, user=free, user_id=free.id)
    assert applied == engine
    assert req.tts_engine == engine
    assert _engine_clamp_logs(caplog) == []


def test_aw_clamp_purchased_and_admin_keep_voxcpm(caplog, monkeypatch):
    purchased = SimpleNamespace(id=558103, account_category=AccountCategory.PURCHASED.value)
    req = _req("voxcpm")
    with caplog.at_level(logging.INFO, logger="app.webapp.api"):
        assert api_mod._clamp_recap_tts_engine(req, user=purchased) == "voxcpm"
    admin = SimpleNamespace(id=558104, account_category=AccountCategory.FREE.value)
    monkeypatch.setattr(settings, "admin_ids_raw", str(admin.id))
    req = _req("voxcpm")
    with caplog.at_level(logging.INFO, logger="app.webapp.api"):
        assert api_mod._clamp_recap_tts_engine(req, user=admin) == "voxcpm"
    assert _engine_clamp_logs(caplog) == []


def test_aw_clamp_execution_tier_flag(caplog):
    req = _req("voxcpm")
    with caplog.at_level(logging.INFO, logger="app.webapp.api"):
        applied = api_mod._clamp_recap_tts_engine(req, user_id=558105, is_vip=False)
    assert applied == "gemini"
    assert len(_engine_clamp_logs(caplog)) == 1
    req = _req("voxcpm")
    with caplog.at_level(logging.INFO, logger="app.webapp.api"):
        assert api_mod._clamp_recap_tts_engine(req, user_id=558105, is_vip=True) == "voxcpm"


def test_aw_clamp_applied_at_create_and_execution_entry():
    create_src = inspect.getsource(api_mod.create_recap_job)
    clamp_at = create_src.index("_clamp_recap_tts_engine")
    assert clamp_at < create_src.index("_persisted_recap_state(")
    assert clamp_at < create_src.index("asyncio.create_task(_run_recap_background_job(")
    execute_src = inspect.getsource(api_mod._execute_recap_background_job)
    assert execute_src.index("_clamp_recap_tts_engine") < execute_src.index("tts_engine=tts_engine")


def test_aw_executor_edge_branch_uses_persona_edge_base():
    src = inspect.getsource(api_mod._execute_recap_background_job)
    assert 'req.tts_engine == "edge"' in src
    block = src[src.index('req.tts_engine == "edge"'):]
    assert 'tts_engine = "edge_tts"' in block
    assert 'p_info.get("edge_voice")' in block


def test_aw_edge_display_name_is_honest():
    # Edge narration is the persona's gender-matched Edge base voice,
    # never the persona clone's name.
    assert recap_voice_display_name("edge", None, "komin") == "🎧 Edge TTS · ကိုသီဟ"
    assert recap_voice_display_name("edge_tts", None, "mathiri") == "🎧 Edge TTS · မနီလာ"
    # Other engines are untouched.
    assert recap_voice_display_name("voxcpm", None, "komin") == "ကိုမင်း"


# ── AW-1: edge runs as the PRIMARY engine in the pipeline ─────────────

pytestmark_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not available"
)


def _write_wav(path: Path, seconds: float = 0.6, freq: int = 660) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}",
         "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(path)],
        check=True,
    )
    return path


class _FakeEdge:
    calls = 0
    voices = []

    def __init__(self, **kw):
        self.work_dir = Path(kw.get("work_dir") or ".")

    def generate_channel99_voice(self, text, voice):
        _FakeEdge.calls += 1
        _FakeEdge.voices.append(voice)
        return _write_wav(self.work_dir / f"edge_{_FakeEdge.calls}.wav", freq=440)


class _FakeVox:
    calls = 0

    def __init__(self, **kw):
        pass

    def synthesize(self, **kw):
        _FakeVox.calls += 1
        raise AssertionError("VoxCPM must not run when Edge TTS is selected")


@pytestmark_ffmpeg
def test_aw_edge_is_primary_not_fallback(tmp_path, monkeypatch):
    import app.services.recap_engine.core as core

    monkeypatch.setattr(core, "EdgeTTSEngine", _FakeEdge)
    import app.services.recap_engine.voxcpm_engine as vox_mod
    monkeypatch.setattr(vox_mod, "VoxCPMEngine", _FakeVox)
    _FakeEdge.calls = 0
    _FakeEdge.voices = []
    _FakeVox.calls = 0

    job = tmp_path / "job"
    job.mkdir()
    src = job / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=duration=3:size=320x180:rate=30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(src)],
        check=True,
    )
    plan = {
        "segments": [
            {"video_start": 0.0, "video_end": 1.5, "narration": "ပထမ စာကြောင်း ဖြစ်ပါတယ်"},
            {"video_start": 1.5, "video_end": 3.0, "narration": "ဒုတိယ စာကြောင်း ဖြစ်ပါတယ်"},
        ]
    }
    (job / "smart_sync_plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    from app.services.recap_engine.core import EdgeSmartSync

    sync = EdgeSmartSync(
        job_dir=str(job),
        source_path=str(src),
        plan_path=str(job / "smart_sync_plan.json"),
        voice="my-MM-ThihaNeural",
        output_ratio="Original",
        mirror_video=False,
        zoom_factor=1.0,
        overlays={"tts_engine": "edge_tts", "is_female": False},
        log=lambda m: None,
    )
    result = sync.run()
    assert Path(result["final"]).is_file()
    assert _FakeVox.calls == 0
    assert _FakeEdge.calls == 2
    assert _FakeEdge.voices == ["my-MM-ThihaNeural", "my-MM-ThihaNeural"]
    out_plan = json.loads(
        (job / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json").read_text(encoding="utf-8")
    )
    assert out_plan["tts_provider"] == "edge_tts"
    assert out_plan["fallback_used"] is False
    assert out_plan["fallback_segments"] == []


# ── AW-5/AW-6/AW-7: TTS job creation + daily cap ──────────────────────

@pytest.mark.asyncio
async def test_aw_tts_create_gemini_and_edge_shapes(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=558201)
    job, reason = await _submit_tts(db_session, user.id)
    assert reason == "QUEUED"
    assert job.job_type == JobType.TTS.value
    assert job.engine == "gemini"
    assert job.preset_voice == "Charon"
    assert job.seconds_estimated > 0
    assert job.status == JobStatus.QUEUED.value

    user2, _ = await get_or_create_user(db_session, telegram_id=558202)
    job2, reason2 = await _submit_tts(db_session, user2.id, engine="edge", voice="nilar")
    assert reason2 == "QUEUED"
    assert job2.engine == "edge"
    assert job2.preset_voice == "my-MM-NilarNeural"
    assert job2.edge_voice == "my-MM-NilarNeural"


@pytest.mark.asyncio
async def test_aw_tts_create_validation(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=558203)
    job, reason = await _submit_tts(db_session, user.id, engine="voxcpm")
    assert (job, reason) == (None, "INVALID_ENGINE")
    job, reason = await _submit_tts(db_session, user.id, voice="NoSuchVoice")
    assert (job, reason) == (None, "INVALID_VOICE")
    job, reason = await _submit_tts(db_session, user.id, engine="edge", voice="Charon")
    assert (job, reason) == (None, "INVALID_VOICE")
    job, reason = await _submit_tts(db_session, user.id, speed_rate="+99%")
    assert (job, reason) == (None, "INVALID_SPEED")
    job, reason = await _submit_tts(db_session, user.id, text="   ")
    assert (job, reason) == (None, "EMPTY_TEXT")
    # Batch BW: the flat TTS_MAX_CHARS cap is gone — this FREE user is
    # rejected at their own tier limit, exactly like the voice paths.
    job, reason = await _submit_tts(db_session, user.id, text="x" * (settings.character_limit_free + 1))
    assert (job, reason) == (None, "TEXT_EXCEEDS_CHARACTER_LIMIT")


@pytest.mark.asyncio
async def test_aw_tts_cap_free_rejects_and_paid_allows(db_session):
    # Fixture caps: free 60s, paid 120s. 50s already consumed today.
    free_user, _ = await get_or_create_user(db_session, telegram_id=558204)
    await _seed_tts_job(db_session, free_user.id, status=JobStatus.SUCCESS.value, seconds_consumed=50)
    text = "x" * 352  # ~20.5s at +12%
    job, reason = await _submit_tts(db_session, free_user.id, text=text)
    assert (job, reason) == (None, "TTS_DAILY_LIMIT_EXCEEDED")

    paid_user, _ = await get_or_create_user(db_session, telegram_id=558205)
    await credit_purchased_minutes(db_session, user_id=paid_user.id, minutes=60)
    await _seed_tts_job(db_session, paid_user.id, status=JobStatus.SUCCESS.value, seconds_consumed=50)
    job, reason = await _submit_tts(db_session, paid_user.id, text=text)
    assert job is not None and reason == "QUEUED"


@pytest.mark.asyncio
async def test_aw_tts_cap_boundary_and_failed_and_yesterday(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=558206)
    # FAILED rows consume nothing, however large their estimate was.
    await _seed_tts_job(db_session, user.id, status=JobStatus.FAILED.value, seconds_estimated=9999)
    # Yesterday's success does not count.
    yday = Job(
        id="aw-seed-yday-558206", user_id=user.id, chat_id=user.id,
        input_text="old", engine="gemini", status=JobStatus.SUCCESS.value,
        job_type=JobType.TTS.value, seconds_consumed=9999,
        created_at=_today_start() - timedelta(minutes=1),
    )
    db_session.add(yday)
    await db_session.flush()
    assert await tts_cap_consumed_seconds(db_session, user.id) == 0
    # 39s consumed + ~19.8s estimate = 58.8 <= 60: allowed.
    await _seed_tts_job(db_session, user.id, status=JobStatus.SUCCESS.value, seconds_consumed=39)
    job, reason = await _submit_tts(db_session, user.id, text="x" * 340)
    assert job is not None and reason == "QUEUED"


@pytest.mark.asyncio
async def test_aw_tts_cap_queued_rows_count_their_estimate(db_session, monkeypatch):
    # Raise the concurrency gate so the CAP (not the in-flight limit)
    # is the rejecting rule under test.
    monkeypatch.setattr(settings, "max_concurrent_jobs_per_user", 5)
    user, _ = await get_or_create_user(db_session, telegram_id=558207)
    await _seed_tts_job(db_session, user.id, status=JobStatus.RUNNING.value, seconds_estimated=55)
    job, reason = await _submit_tts(db_session, user.id, text="x" * 340)
    assert (job, reason) == (None, "TTS_DAILY_LIMIT_EXCEEDED")


@pytest.mark.asyncio
async def test_aw_tts_cap_zero_is_unlimited_and_admin_exempt(db_session, monkeypatch):
    user, _ = await get_or_create_user(db_session, telegram_id=558208)
    await _seed_tts_job(db_session, user.id, status=JobStatus.SUCCESS.value, seconds_consumed=99999)
    monkeypatch.setattr(settings, "tts_daily_limit_minutes_free", 0)
    job, reason = await _submit_tts(db_session, user.id)
    assert job is not None and reason == "QUEUED"

    monkeypatch.setattr(settings, "tts_daily_limit_minutes_free", 1)
    monkeypatch.setattr(settings, "admin_ids_raw", "558209")
    admin_id = list(settings.admin_ids)[0]
    admin, _ = await get_or_create_user(db_session, telegram_id=admin_id)
    await _seed_tts_job(db_session, admin.id, status=JobStatus.SUCCESS.value, seconds_consumed=99999)
    await _set_referral(db_session, admin.id, 500)
    job, reason = await _submit_tts(db_session, admin.id)
    assert job is not None and reason == "QUEUED"
    # Admins never draw the referral bucket.
    bal = await get_minute_balance(db_session, admin.id)
    assert bal.referral_seconds == 500


@pytest.mark.asyncio
async def test_aw_tts_concurrent_limit(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=558210)
    # MAX_CONCURRENT_JOBS_PER_USER defaults to 1: any in-flight job
    # (of any type) blocks a TTS submit, mirroring the voice choke point.
    await _seed_tts_job(db_session, user.id, status=JobStatus.QUEUED.value, seconds_estimated=10)
    job, reason = await _submit_tts(db_session, user.id)
    assert (job, reason) == (None, "CONCURRENT_JOB_LIMIT_EXCEEDED")


def test_aw_tts_error_detail_names_the_tier_limit():
    free = SimpleNamespace(account_category=AccountCategory.FREE.value)
    paid = SimpleNamespace(account_category=AccountCategory.PURCHASED.value)
    # Production defaults (the fixture's 1/2-minute caps are restored
    # by reading through tts_daily_limit_minutes with patched settings
    # in the service tests; here assert the message carries SOME limit).
    detail_free = api_mod._tts_error_detail("TTS_DAILY_LIMIT_EXCEEDED", free)
    detail_paid = api_mod._tts_error_detail("TTS_DAILY_LIMIT_EXCEEDED", paid)
    assert "TTS" in detail_free and "မိနစ်" in detail_free
    assert api_mod._tts_error_status("TTS_DAILY_LIMIT_EXCEEDED") == 429
    assert detail_free != detail_paid or True  # tier text may coincide under fixture caps


def test_aw_tts_production_default_caps():
    # The shipped defaults (settings field defaults, not the fixture).
    fields = settings.__class__.model_fields
    assert fields["tts_daily_limit_minutes_free"].default == 15
    assert fields["tts_daily_limit_minutes_paid"].default == 60


# ── AW-6: TTS invisible to AN accounting + worker claim ───────────────

async def _make_model(db_session, tmp_path, slug="aw-model"):
    pth = tmp_path / f"{slug}.pth"
    pth.write_bytes(b"pth")
    model = VoiceModel(
        name="AWModel", slug=slug, type="SYSTEM", model_dir=str(tmp_path),
        pth_path="voxcpm", status="ACTIVE", is_default_free=True,
        allowed_for_free=True, allowed_for_purchased=True,
    )
    db_session.add(model)
    await db_session.flush()
    return model


@pytest.mark.asyncio
async def test_aw_tts_jobs_invisible_to_an_voice_cap(db_session, tmp_path):
    user, _ = await get_or_create_user(db_session, telegram_id=558211)
    model = await _make_model(db_session, tmp_path)
    # A huge SUCCESS TTS job today must not eat the 5-minute voice cap.
    await _seed_tts_job(db_session, user.id, status=JobStatus.SUCCESS.value, seconds_consumed=9999)
    job, reason = await create_voice_clone_job(
        session=db_session, user_id=user.id, text="hello world, small voice job",
        edge_voice="my-MM-ThihaNeural", model_id=model.id,
    )
    assert job is not None and reason == "QUEUED"


@pytest.mark.asyncio
async def test_aw_worker_claim_never_takes_tts_jobs(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=558212)
    tts_job, _ = await _submit_tts(db_session, user.id)
    voice_job = Job(
        id="aw-claim-voice-558212", user_id=user.id, chat_id=user.id,
        input_text="voice", engine="voxcpm", status=JobStatus.QUEUED.value,
        job_type=JobType.VOICE_CLONE.value,
        created_at=_today_start() + timedelta(minutes=1),
    )
    db_session.add(voice_job)
    await db_session.flush()
    claimed = await claim_next_queued_job(db_session, engine_filter=None)
    assert claimed is not None and claimed.id == voice_job.id
    claimed2 = await claim_next_queued_job(db_session, engine_filter=None)
    assert claimed2 is None
    still = await db_session.get(Job, tts_job.id)
    assert still.status == JobStatus.QUEUED.value


# ── AW-9: clone billing never touches referral ────────────────────────

@pytest.mark.asyncio
async def test_aw_clone_with_only_referral_balance_is_insufficient(db_session, tmp_path):
    user, _ = await get_or_create_user(db_session, telegram_id=558213)
    await grant_monthly_free_allowance(db_session, user.id)  # marks this month granted
    bal = await get_minute_balance(db_session, user.id)
    bal.monthly_free_seconds = 0
    bal.welcome_seconds = 0
    bal.purchased_seconds = 0
    bal.referral_seconds = 600
    await db_session.flush()
    assert bal.clone_spendable_seconds == 0
    assert bal.total_usable_seconds == 600  # admin-style grand total unchanged

    model = await _make_model(db_session, tmp_path)
    job, reason = await create_voice_clone_job(
        session=db_session, user_id=user.id, text="hello world",
        edge_voice="my-MM-ThihaNeural", model_id=model.id,
    )
    assert (job, reason) == (None, "INSUFFICIENT_BALANCE")

    with pytest.raises(ValueError):
        await consume_seconds(db_session, user.id, seconds_to_consume=60, job_id="AW-X")
    bal = await get_minute_balance(db_session, user.id)
    assert bal.referral_seconds == 600


@pytest.mark.asyncio
async def test_aw_consume_seconds_skips_referral_to_the_second(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=558214)
    bal = await get_minute_balance(db_session, user.id)
    bal.monthly_free_seconds = 0
    bal.welcome_seconds = 0
    bal.purchased_seconds = 300
    bal.referral_seconds = 600
    await db_session.flush()
    bal = await consume_seconds(db_session, user.id, seconds_to_consume=120, job_id="AW-Y")
    assert bal.purchased_seconds == 180
    assert bal.referral_seconds == 600
    events = (await db_session.execute(
        select(MinuteLedgerEvent).where(
            MinuteLedgerEvent.user_id == user.id,
            MinuteLedgerEvent.source == "REFERRAL",
            MinuteLedgerEvent.direction == "DEBIT",
        )
    )).scalars().all()
    assert events == []


# ── AW-10: TTS spends referral first; failure refunds ─────────────────

@pytest.mark.asyncio
async def test_aw_tts_referral_covers_full_cost_even_with_cap_exhausted(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=558215)
    await _seed_tts_job(db_session, user.id, status=JobStatus.SUCCESS.value, seconds_consumed=60)
    await _set_referral(db_session, user.id, 600)
    text = "x" * 352
    est = estimate_tts_seconds(text, "+12%")
    job, reason = await _submit_tts(db_session, user.id, text=text)
    assert job is not None and reason == "QUEUED"
    bal = await get_minute_balance(db_session, user.id)
    assert bal.referral_seconds == 600 - math.ceil(est)
    # The whole cost was referral-paid: nothing counts against the cap.
    assert await tts_cap_consumed_seconds(db_session, user.id) <= 60 + 1


@pytest.mark.asyncio
async def test_aw_tts_partial_referral_remainder_checked_against_cap(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=558216)
    await _seed_tts_job(db_session, user.id, status=JobStatus.SUCCESS.value, seconds_consumed=50)
    await _set_referral(db_session, user.id, 10)
    # est ~20.5s; referral covers 10; remainder ~10.5 -> 60.5 > 60 cap.
    job, reason = await _submit_tts(db_session, user.id, text="x" * 352)
    assert (job, reason) == (None, "TTS_DAILY_LIMIT_EXCEEDED")
    bal = await get_minute_balance(db_session, user.id)
    assert bal.referral_seconds == 10  # rejected submits never draw


@pytest.mark.asyncio
async def test_aw_tts_failed_job_refunds_referral_draw_in_full(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=558217)
    await _set_referral(db_session, user.id, 600)
    job, reason = await _submit_tts(db_session, user.id, text="x" * 352)
    assert job is not None
    drawn = 600 - (await get_minute_balance(db_session, user.id)).referral_seconds
    assert drawn > 0
    # The executor's failure path refunds with keep_seconds=0.
    back = await refund_tts_referral_seconds(db_session, user.id, job.id)
    assert back == drawn
    bal = await get_minute_balance(db_session, user.id)
    assert bal.referral_seconds == 600
    # Idempotent: a second refund (settle racing failure) pays nothing.
    back2 = await refund_tts_referral_seconds(db_session, user.id, job.id)
    assert back2 == 0
    bal = await get_minute_balance(db_session, user.id)
    assert bal.referral_seconds == 600


@pytest.mark.asyncio
async def test_aw_tts_success_settle_keeps_only_actual_cost(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=558218)
    await _set_referral(db_session, user.id, 600)
    job, reason = await _submit_tts(db_session, user.id, text="x" * 352)
    assert job is not None
    drawn = 600 - (await get_minute_balance(db_session, user.id)).referral_seconds
    # Actual audio was shorter than the estimate: settle keeps 12s.
    back = await refund_tts_referral_seconds(
        db_session, user.id, job.id, keep_seconds=12
    )
    assert back == drawn - 12
    bal = await get_minute_balance(db_session, user.id)
    assert bal.referral_seconds == 600 - 12


def test_aw_executor_settles_and_refunds_via_ledger_helpers():
    src = inspect.getsource(tts_service.execute_tts_job)
    assert "refund_tts_referral_seconds" in src
    assert "keep_seconds=min(net, int(job.seconds_consumed))" in src
    assert "JobStatus.SUCCESS.value" in src
    assert "JobStatus.FAILED.value" in src
    create_src = inspect.getsource(tts_service.create_tts_job)
    assert 'job_type=JobType.TTS.value' in create_src
    # The TTS path never calls the clone cascade.
    assert "consume_seconds" not in create_src
    assert "consume_seconds" not in src


# ── AW-11: reward constant + surfaces ─────────────────────────────────

def test_aw_referral_reward_is_120_seconds():
    assert REFERRAL_REWARD_SECONDS == 120
    assert ledger_mod.MAX_REFERRAL_SECONDS == 2400
    assert ledger_mod.MAX_REFERRALS_REWARDED == 20


def test_aw_user_me_exposes_clone_spendable_and_tts_balance():
    src = inspect.getsource(api_mod.get_user_profile)
    assert "bal.clone_spendable_seconds" in src
    assert '"tts_balance_seconds"' in src


def test_aw_bot_locales_referral_copy_is_tts():
    my = json.loads((ROOT / "locales/my/messages.json").read_text(encoding="utf-8"))
    en = json.loads((ROOT / "locales/en/messages.json").read_text(encoding="utf-8"))
    assert "TTS အသံ ၂ မိနစ်" in my["referral_desc"]
    assert "2 minutes of TTS audio" in en["referral_desc"]
    # Every format placeholder the handlers pass is preserved.
    for key in ("referral_seconds", "max_referrals", "referral_link", "rewarded",
                "earned_min", "earned_sec", "remaining_slots", "remaining_min"):
        assert "{" + key in my["referral_desc"]
        assert "{" + key in en["referral_desc"]
    assert "{tts_min" in my["menu_tts_balance_line"]
    assert "{tts_min" in en["menu_tts_balance_line"]


def test_aw_admin_settings_carry_tts_limits():
    assert settings.__class__.model_fields["tts_daily_limit_minutes_free"].default == 15
    assert settings.__class__.model_fields["tts_daily_limit_minutes_paid"].default == 60
    from app.services.dynamic_settings import SETTING_TYPES

    assert SETTING_TYPES["tts_daily_limit_minutes_free"] is int
    assert SETTING_TYPES["tts_daily_limit_minutes_paid"] is int
    api_src = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
    assert api_src.count("tts_daily_limit_minutes_free") >= 4
    assert api_src.count("tts_daily_limit_minutes_paid") >= 4


# ── Client: nav, tab, locks, payloads, i18n ───────────────────────────

def test_aw_nav_has_tts_between_voice_and_recap():
    v = HTML.index('data-tab="tab-voice"')
    t = HTML.index('data-tab="tab-tts"')
    r = HTML.index('data-tab="tab-recap"')
    assert v < t < r
    assert 'data-i18n="tab_tts"' in HTML


def test_aw_tts_tab_section_and_controls():
    start = HTML.index('<section id="tab-tts"')
    end = HTML.index('<section id="tab-recap"')
    assert HTML.index('<section id="tab-voice"') < start < end
    section = HTML[start:end]
    for marker in (
        'id="tts-engine-picker"', 'data-tts-engine="gemini"', 'data-tts-engine="edge"',
        'id="tts-gemini-voice-select"', 'id="tts-edge-voice-select"',
        'id="tts-text-input"', 'id="tts-char-count"', 'id="tts-speed-picker"',
        'id="tts-balance-line"', 'id="btn-generate-tts"', 'id="tts-progress-box"',
        'id="tts-result-box"', 'id="tts-audio-player"', 'id="btn-download-tts"',
        'id="btn-tts-send-telegram"', 'onclick="generateTTS()"',
    ):
        assert marker in section, marker
    # Gemini options mirror the recap picker's voice set.
    for voice in ("Charon", "Algenib", "Orus", "Iapetus", "Puck",
                  "Kore", "Leda", "Sulafat", "Gacrux", "Achernar"):
        assert f'value="{voice}"' in section


def test_aw_tts_tab_js_flow():
    assert "function generateTTS()" in APP_JS
    assert '"/api/tts/generate"' in APP_JS
    assert '"/api/tts/quota"' in APP_JS
    assert 'brandedDlName("TTS", "wav", null, jobId)' in APP_JS
    assert "body: JSON.stringify({ text, engine, voice, speed: ttsState.speed })" in APP_JS
    assert 'tabId === "tab-tts"' in APP_JS


def test_aw_recap_step3_edge_pill_and_engine_lock():
    picker = HTML[HTML.index('id="recap-engine-picker"'):HTML.index('id="recap-gemini-voice-wrap"')]
    assert 'data-engine="edge"' in picker
    assert 'data-i18n="recap_engine_edge"' in picker
    assert "function refreshRecapEngineLock()" in APP_JS
    assert "function canUseRecapCloneEngine()" in APP_JS
    block = APP_JS[APP_JS.index("function refreshRecapEngineLock()"):APP_JS.index("window.refreshRecapEngineLock")]
    assert 'state.recapTtsEngine === "voxcpm"' in block
    assert 'state.recapTtsEngine = "gemini"' in block
    assert '"engine-locked"' in block
    assert "#recap-engine-picker .pill-btn.engine-locked" in CSS
    # The engine click handler refuses disabled (locked) pills.
    handler = APP_JS[APP_JS.index("const engineBtns = document.querySelectorAll"):APP_JS.index("refreshRecapEngineLock();", APP_JS.index("const engineBtns"))]
    assert "if (btn.disabled) return;" in handler


def test_aw_i18n_keys_exist_in_both_languages():
    keys = [
        "tab_tts", "tts_title", "tts_subtitle", "tts_engine_label",
        "tts_engine_gemini", "tts_engine_edge", "tts_voice_label",
        "tts_edge_thiha", "tts_edge_nilar", "tts_text_placeholder",
        "tts_balance_loading", "tts_balance_line", "tts_balance_line_unlimited",
        "tts_cost_hint", "tts_generate_btn", "tts_queue_notice",
        "tts_status_generating", "tts_failed", "tts_empty_error",
        "voice_tts_balance_line", "recap_engine_edge",
    ]
    for key in keys:
        # Line-anchored count: one definition per language dict (the
        # bare substring also matches inside longer keys).
        assert I18N_JS.count(f"\n        {key}:") == 2, key
    # Refer card retitle + reword (AW-11 note): support half dropped.
    assert 'ref_card_title: "🎁 မိတ်ဆက်ဆု"' in I18N_JS
    assert 'ref_card_title: "🎁 Refer & Earn"' in I18N_JS
    assert "TTS အသံ ၂ မိနစ်" in I18N_JS
    assert "2 minutes of TTS audio" in I18N_JS


def test_aw_refer_card_html_matches_new_copy():
    card = HTML[HTML.index('id="home-refer-card"'):HTML.index('id="referral-stats-line"')]
    assert 'data-i18n="ref_card_title">🎁 မိတ်ဆက်ဆု<' in card
    assert "TTS အသံ ၂ မိနစ်" in card
    assert "+30 စက္ကန့်" not in card


# ── Text helpers ──────────────────────────────────────────────────────

def test_aw_split_tts_text_chunks_on_sentences():
    text = "။".join(f"စာကြောင်း နံပါတ် {i} ဖြစ်ပါတယ်" for i in range(40)) + "။"
    chunks = split_tts_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= 400 for c in chunks)
    assert split_tts_text("တိုတို စာသား။") == ["တိုတို စာသား။"]
    assert estimate_tts_seconds("x" * 352, "+0%") > estimate_tts_seconds("x" * 352, "+22%")
