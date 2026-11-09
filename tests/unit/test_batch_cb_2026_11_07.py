"""Batch CB — honest recap ETA (2026-11-07).

Owner complaint (live screenshots 2026-11-07 11:30): the wizard
showed "estimated ~1 minute" while a recap job had been RUNNING for
3,034 s; a sibling FAILED at 4,030 s. Root causes pinned here so
they cannot return:

* submit-time text was hardcoded fiction ("1.5 - 2.5" / "3 - 4"
  minutes for every shape) — now a server-computed range for the
  job's actual shape, or NO number when there is no basis;
* the in-job ETA was ONE global median x (1 - progress%) — now
  shape-conditioned medians (engine class x duration band) and a
  stage decomposition whose narration remainder scales with
  segments remaining x the job's OWN observed pace (floored at the
  calibrated typical), so a job deep in narration cannot collapse
  to "~1 minute";
* the client floored the display at 1 minute — the floor is gone;
  ranges are shown as ranges, and past the initial estimate's high
  bound the surfaces switch to the overrun sentence;
* Item 6 (owner add-on): the AI fact sheet pairs "no FREE daily
  voice cap" with the governing monthly allowance + a steering
  sentence, only while the cap is retired.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.session as session_mod
from app.config import settings
from app.db.base import Base
from app.models.enums import JobStatus, JobType
from app.models.schema import Job, User
from app.services import group_ai_admin as gai
from app.services import recap_eta
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
API_SRC = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_eta_state():
    before = set(api_mod.RECAP_JOBS_REGISTRY.keys())
    api_mod._RECAP_ETA_MEDIAN_CACHE.update({"at": 0.0, "seconds": None, "medians": None})
    yield
    for jid in set(api_mod.RECAP_JOBS_REGISTRY.keys()) - before:
        api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)
    api_mod._RECAP_ETA_MEDIAN_CACHE.update({"at": 0.0, "seconds": None, "medians": None})


async def _open_store(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/batch_cb.db", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    return engine, maker


def _success_job(jid, uid, seconds, *, tts_engine=None, source_duration=None, base=None):
    base = base or datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    cfg = None
    if tts_engine is not None:
        req = api_mod.RecapConfigRequest(
            url="https://example.com/v", title="t", tts_engine=tts_engine,
            duration_seconds=int(source_duration or 60),
        )
        cfg = json.dumps({
            "config": req.model_dump(),
            "source_duration_seconds": float(source_duration or 0),
        })
    return Job(
        id=jid, user_id=uid, chat_id=uid,
        status=JobStatus.SUCCESS.value,
        job_type=JobType.RECAP.value, engine="recap",
        preset_voice="komin", input_text="done",
        recap_config=cfg,
        seconds_estimated=int(source_duration or 0),
        created_at=base,
        started_at=base,
        finished_at=base + timedelta(seconds=seconds),
    )


# ── Item 7(a)/(b): calibration submit ranges for canonical shapes ──

def test_calibration_range_short_gemini_is_about_1_to_4_minutes():
    segs = recap_eta.estimate_segments(19)
    low, high = recap_eta.total_range("gemini", 19, segs, "standard")
    # The measured 19 s Gemini job took ~2 min end-to-end
    # (2026-11-05 report): the calibrated range brackets it and
    # sits inside the spec's ~1-4 minute span for this shape.
    assert 45 <= low <= 150
    assert 120 <= high <= 300
    assert low < high


def test_calibration_range_voxcpm_38_segments_covers_25_to_45_minutes():
    low, high = recap_eta.total_range("voxcpm", 600, 38, "standard")
    # Covers the spec's ~25-45 min span for the canonical shape.
    assert low <= 25 * 60
    assert high >= 45 * 60


# ── Item 7(c): RUNNING narration remaining — the screenshot killer ──

def test_running_narration_remaining_scales_with_observed_pace():
    low, high, point = recap_eta.remaining_range(
        "tts", engine="voxcpm", duration_seconds=600, quality="standard",
        segments_total=38, segments_done=20, observed_pace=60.0,
    )
    # 18 segments left at the job's own 60 s/segment = 1,080 s of
    # narration alone, plus the render tail. Explicitly NOT ~1 min.
    assert point >= 18 * 60
    assert point > 1200
    assert low >= 1000
    assert high >= point


def test_running_narration_pace_is_floored_at_typical():
    # A fast start (5 s/segment observed) must not promise an
    # impossible finish: the point pace floors at the calibrated
    # typical 35 s/segment for VoxCPM.
    _low, _high, point = recap_eta.remaining_range(
        "tts", engine="voxcpm", duration_seconds=600, quality="standard",
        segments_total=38, segments_done=20, observed_pace=5.0,
    )
    assert point >= 18 * 35


def test_running_narration_without_marks_uses_typical_pace():
    _low, _high, point = recap_eta.remaining_range(
        "tts", engine="edge", duration_seconds=300, quality="standard",
        segments_total=24, segments_done=12, observed_pace=None,
    )
    # Edge typical pace is 2.5 s/segment: 12 left = 30 s of narration
    # plus the sync/finalize tail (the measured dominant cost for
    # Edge jobs) — a few minutes, never a runaway number.
    assert 30 <= point < 600


# ── Item 1: resolver fallback chain (bucket -> engine -> global) ────

def _medians(buckets=None, engines=None, glob=None):
    return {"buckets": buckets or {}, "engines": engines or {}, "global": glob}


def test_resolver_prefers_bucket_then_engine_then_global():
    medians = _medians(
        buckets={("voxcpm", "le600"): (2000.0, 6)},
        engines={"voxcpm": (1500.0, 9), "gemini": (900.0, 5)},
        glob=(600.0, 20),
    )
    got = api_mod._recap_resolve_total_range(medians, "voxcpm", 300, "standard", require_samples=True)
    assert got[2] == 2000.0 and got[3] == "bucket"
    got = api_mod._recap_resolve_total_range(medians, "gemini", 300, "standard", require_samples=True)
    assert got[2] == 900.0 and got[3] == "engine"
    got = api_mod._recap_resolve_total_range(medians, "edge", 300, "standard", require_samples=True)
    assert got[2] == 600.0 and got[3] == "global"


def test_resolver_thresholds_and_calibration_cold_start():
    thin = _medians(buckets={("voxcpm", "le600"): (2000.0, 4)}, glob=(600.0, 2))
    # 4 bucket samples and 2 global samples are below the trust
    # thresholds: the in-job resolver returns NO estimate...
    assert api_mod._recap_resolve_total_range(
        thin, "voxcpm", 300, "standard", require_samples=True
    ) is None
    # ...while the pre-submit resolver cold-starts from calibration.
    got = api_mod._recap_resolve_total_range(
        thin, "voxcpm", 300, "standard", require_samples=False
    )
    assert got is not None and got[3] == "calibration"
    # No duration -> no basis -> no number even pre-submit.
    assert api_mod._recap_resolve_total_range(
        thin, "voxcpm", 0, "standard", require_samples=False
    ) is None


# ── Item 7(d): QUEUED waves on the job's OWN bucket median ──────────

def test_queued_eta_waves_use_bucket_total():
    totals = (1400.0, 2900.0, 2000.0, "bucket")
    out = api_mod._recap_eta_payload(
        status="QUEUED", position=2, totals=totals, submit_totals=totals,
    )
    # Position 2 = 2 waves at semaphore width 2, on the bucket's own
    # 2,000 s — not on any global blend.
    assert out["eta_seconds"] == 4000
    assert out["eta_range_seconds"] == [2800, 5800]
    assert out["eta_overrun"] is False


# ── Item 7(e): the overrun flag flips past the estimate's high bound ─

def test_overrun_flips_for_running_and_queued():
    medians = _medians(glob=(600.0, 3))  # submit high bound = 600 x 1.45 = 870
    now = time.time()
    stale = {
        "user_id": 1, "status": "RUNNING", "created_at": now - 5000,
        "eta_run_started_at": now - 5000, "tts_engine": "gemini",
        "source_duration_seconds": 60.0, "progress_stage": "download",
    }
    out = api_mod._recap_eta_from_entry(stale, medians, 0, now=now)
    assert out["eta_overrun"] is True
    fresh = dict(stale, created_at=now, eta_run_started_at=now)
    out = api_mod._recap_eta_from_entry(fresh, medians, 0, now=now)
    assert out["eta_overrun"] is False
    queued = {
        "user_id": 1, "status": "QUEUED", "created_at": now - 2000,
        "tts_engine": "gemini", "source_duration_seconds": 60.0,
    }
    out = api_mod._recap_eta_from_entry(queued, medians, 0, now=now)
    assert out["eta_overrun"] is True  # 2,000 s queued > 1 wave x 870


# ── Item 1: bucketing from real Job rows ────────────────────────────

@pytest.mark.asyncio
async def test_median_buckets_attribute_rows_by_shape(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 880601
        async with maker() as s:
            s.add(User(id=uid, language_code="my", recap_credits=0))
            # 5 voxcpm / ~300 s jobs at 2,000 s each: a trusted bucket.
            for i in range(5):
                s.add(_success_job(
                    f"rc_cb_b_{i}", uid, 2000,
                    tts_engine="voxcpm", source_duration=300,
                    base=datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc) + timedelta(hours=i),
                ))
            # 3 gemini jobs: below the engine trust threshold.
            for i in range(3):
                s.add(_success_job(
                    f"rc_cb_g_{i}", uid, 400,
                    tts_engine="gemini", source_duration=30,
                    base=datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc) + timedelta(hours=i),
                ))
            await s.commit()
        medians = await api_mod._recap_median_buckets()
        assert medians["buckets"][("voxcpm", "le600")] == (2000.0, 5)
        assert medians["engines"]["gemini"] == (400.0, 3)
        got = api_mod._recap_resolve_total_range(
            medians, "voxcpm", 300, "standard", require_samples=True
        )
        assert got[2] == 2000.0 and got[3] == "bucket"
        # Gemini has only 3 samples at every level above global: the
        # global level (8 samples) answers instead of the thin engine.
        got = api_mod._recap_resolve_total_range(
            medians, "gemini", 30, "standard", require_samples=True
        )
        assert got[3] == "global"
    finally:
        await engine.dispose()


# ── Item 2/4: status payload for the screenshot's job shape ────────

@pytest.mark.asyncio
async def test_status_running_narration_eta_is_not_one_minute(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 880602
        async with maker() as s:
            s.add(User(id=uid, language_code="my", recap_credits=0))
            for i in range(3):
                s.add(_success_job(f"rc_cb_s_{i}", uid, 600))
            await s.commit()
        now = time.time()
        api_mod.RECAP_JOBS_REGISTRY["rc_cb_live"] = {
            "user_id": uid, "status": "RUNNING", "created_at": now - 760,
            "eta_run_started_at": now - 760,
            "tts_engine": "voxcpm", "render_quality": "standard",
            "source_duration_seconds": 600.0,
            "progress_percent": 74, "progress_stage": "tts",
            "progress_sub": "20/38", "segments_count": 38,
            "eta_stage": "tts", "eta_stage_since": now - 700,
            "eta_mark_first": (2, now - 700), "eta_mark_last": (20, now, 38),
        }
        async with maker() as s:
            res = await api_mod.get_recap_status("rc_cb_live", user=User(id=uid), db=s)
        # Observed pace from the marks: 700 s / 18 segments ≈ 39
        # s/segment; 18 remain -> ~700 s of narration alone, plus
        # the render tail. The run (760 s) is still inside the
        # submit estimate's high bound, so no overrun yet.
        assert res["eta_range_seconds"] is not None
        assert res["eta_seconds"] >= 800
        assert res["eta_range_seconds"][1] >= res["eta_seconds"]
        assert res["eta_overrun"] is False
    finally:
        await engine.dispose()


# ── Item 3: the Step-4 estimate endpoint ────────────────────────────

@pytest.mark.asyncio
async def test_eta_estimate_endpoint_cold_start_and_no_basis(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        user = User(id=880603, language_code="my")
        res = await api_mod.get_recap_eta_estimate(
            duration_seconds=600, tts_engine="voxcpm",
            render_quality="standard", user=user,
        )
        low, high = res["eta_range_seconds"]
        # Pre-submit the segment count is still a planning estimate
        # (density varies with speech content), so the honest range
        # STRADDLES the canonical 25-45 min span rather than sitting
        # inside it — and is never a small fiction either.
        assert 900 <= low <= 45 * 60
        assert high >= 45 * 60
        res = await api_mod.get_recap_eta_estimate(
            duration_seconds=0, tts_engine="voxcpm",
            render_quality="standard", user=user,
        )
        assert res["eta_range_seconds"] is None
    finally:
        await engine.dispose()


# ── Item 4: the live-jobs payload carries range + overrun ───────────

def test_live_payload_carries_eta_fields_and_overrun():
    now = datetime.now(timezone.utc)
    job = Job(
        id="rc_cb_live_row", user_id=880604, chat_id=880604,
        status=JobStatus.RUNNING.value, job_type=JobType.RECAP.value,
        engine="recap", preset_voice="komin", input_text="t",
        created_at=now - timedelta(seconds=5000),
        started_at=now - timedelta(seconds=5000),
    )
    entry = {
        "user_id": 880604, "status": "RUNNING",
        "created_at": time.time() - 5000,
        "eta_run_started_at": time.time() - 5000,
        "tts_engine": "gemini", "source_duration_seconds": 60.0,
        "progress_percent": 20, "progress_stage": "download",
        "progress_step": "1/4",
    }
    medians = _medians(glob=(600.0, 3))
    payload = api_mod._live_job_payload(job, entry, now=now, medians=medians)
    assert payload["eta_overrun"] is True
    assert payload["eta_range_seconds"] is not None


# ── Item 2 wiring pins: marks + entry shape fields ──────────────────

def test_eta_reporting_wiring_pins():
    # The progress callback stamps the ETA marks; the authored-label
    # seam keeps them in step; entries carry the ETA shape.
    assert "_recap_eta_note_progress(entry, stage, sub)" in API_SRC
    assert "_recap_eta_note_progress(entry, stage, \"\")" in API_SRC
    assert '"tts_engine": req.tts_engine' in API_SRC
    assert '"tts_engine": cfg.tts_engine' in API_SRC
    assert '"render_quality": req.render_quality' in API_SRC
    assert '@api_router.get("/recap/eta-estimate")' in API_SRC
    assert '"eta_range_seconds"' in API_SRC
    assert '"eta_overrun"' in API_SRC


# ── Item 7(f): client pins — fiction + floor gone, range surfaces ──

def test_client_hardcoded_estimate_and_floor_are_gone():
    assert "estMinutes" not in APP_JS
    assert "၁.၅ - ၂.၅" not in APP_JS
    assert "recapFormEtaSeconds" not in APP_JS
    assert "Math.max(1, Math.round(recapFormEtaSeconds / 60))" not in APP_JS
    # The step line renders the remaining RANGE through the shared
    # formatter; the Step-4 line asks the server for the shape range.
    assert 'formatEtaRangeText(recapFormEtaRange, "remaining")' in APP_JS
    assert "formatEtaRangeText(data.eta_range_seconds, \"estimate\")" in APP_JS
    assert "/api/recap/eta-estimate" in APP_JS
    assert 'id="recap-step4-eta"' in INDEX_HTML
    # Overrun copy reaches the form, the pill and the sheet.
    assert "liveJobOverrunText(job)" in APP_JS
    assert "liveJobOverrunText(lead)" in APP_JS
    assert 'tf("recap_eta_overrun"' in APP_JS


def test_eta_i18n_keys_exist_in_both_locales():
    for key in (
        "recap_eta_range", "recap_eta_estimate_range",
        "recap_eta_under_minute", "recap_eta_overrun",
    ):
        assert I18N_JS.count(f'{key}:') >= 2, key
    # Placeholders survive in both locales for the ranged keys.
    assert "{low}" in I18N_JS and "{high}" in I18N_JS
    assert "{stage}" in I18N_JS and "{elapsed}" in I18N_JS


# ── Item 6: fact-sheet steering clause ──────────────────────────────

def test_fact_sheet_pairs_no_daily_cap_with_monthly_allowance():
    sheet = gai.build_fact_sheet()
    assert int(settings.voice_daily_limit_minutes_free) == 0
    # Both truths in ONE statement, monthly rendered live (180 s).
    assert "တစ်နေ့ သီးခြား အကန့်အသတ် မရှိ" in sheet
    assert "လစဉ် အခမဲ့ 180 စက္ကန့် (3 မိနစ်) ကသာ အမှန် ကန့်သတ်ထားသည်" in sheet
    # The steering sentence instructs the AI to name the monthly cap.
    assert "သတိပြုရန် (Free Voice Clone)" in sheet
    assert "အမြဲ တွဲ၍ ပြောပါ" in sheet


def test_fact_sheet_steering_gone_when_admin_sets_cap(monkeypatch):
    monkeypatch.setattr(settings, "voice_daily_limit_minutes_free", 9)
    sheet = gai.build_fact_sheet()
    assert "Free တစ်နေ့ 9 မိနစ်" in sheet
    assert "သတိပြုရန် (Free Voice Clone)" not in sheet
    assert "အမှန် ကန့်သတ်ထားသည်" not in sheet


# ── Release pin: cache token (split-literal, sweep-proof) ──────────

def test_cache_token_is_r121():
    token = "20261104_r" + "121"
    stale = "20261104_r" + "120"
    assert INDEX_HTML.count(token) == 4
    assert stale not in INDEX_HTML
