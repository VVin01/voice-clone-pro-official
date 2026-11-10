"""Batch AB (2026-11-04): overlay OOM loop fixes.

Incident: recap job rc_1793799063_8cc5b7 looped forever — it reached
the Production overlay pass, the Render instance was OOM-killed
(2 GB) ~15 s into the pass, the service restarted, the orphan sweep
re-queued the stranded row, repeat. Three fixes are pinned here:

1. LOOP BREAKER — the orphan sweep counts rescues durably in the Job
   row's persisted recap blob (``stranded_reset_count``) and, at the
   limit, FAILS the job (refund exactly once through the persisted
   claim path, failure DM, media strip) instead of re-queueing it.
2. NVENC PROBE — encoder selection trusts a real 1-frame test encode
   (cached per process), not the `ffmpeg -encoders` listing, so a
   GPU-less host never pays a doomed NVENC attempt and a checkpoint
   remembered on a GPU host cannot resurrect it.
3. OVERLAY MEMORY DIET — apply_production_overlays runs as two
   sequential steps (blur boxes, then the rest) with an explicit
   "-threads 2" cap after the quality args; geometry/outputs are
   unchanged.
"""
from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.session as session_mod
from app.config import settings
from app.db.base import Base
from app.models.enums import JobStatus, JobType
from app.models.schema import Job, RecapCreditLedgerEvent, User, utc_now
from app.services.recap_engine import core
from app.webapp import api as api_mod
from app.webapp.server import reset_stale_running_recap_jobs


# ── helpers ──────────────────────────────────────────────────────────

async def _open_store(tmp_path, monkeypatch):
    """Point the API/server helpers' own session factory at a real
    file DB (same harness as test_script_ready_persistence)."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/batch_ab.db", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    monkeypatch.setattr(settings, "admin_ids_raw", "424242")
    monkeypatch.setattr(settings, "bot_token", "")
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    return engine, maker


def _recap_state(**over):
    cfg = api_mod.RecapConfigRequest(
        url="https://example.com/movie",
        title="Loop Movie",
        voice_slug="mahnin",
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


def _job(job_id, user_id, state, status=JobStatus.RUNNING.value, minutes_ago=30):
    now = utc_now()
    return Job(
        id=job_id,
        user_id=user_id,
        chat_id=user_id,
        status=status,
        job_type=JobType.RECAP.value,
        engine="recap",
        preset_voice="mahnin",
        input_text="Loop Movie",
        source_audio_path="https://example.com/movie",
        recap_config=json.dumps(state) if state is not None else None,
        created_at=now - timedelta(minutes=minutes_ago),
        started_at=now - timedelta(minutes=minutes_ago) if status == JobStatus.RUNNING.value else None,
    )


async def _restrand(maker, job_id):
    """Put a row back into the stranded state a restart leaves behind."""
    async with maker() as s:
        job = await s.get(Job, job_id)
        job.status = JobStatus.RUNNING.value
        job.started_at = utc_now() - timedelta(minutes=30)
        await s.commit()


async def _stored(maker, job_id):
    async with maker() as s:
        job = await s.get(Job, job_id)
        state = None
        if job.recap_config:
            try:
                state = json.loads(job.recap_config)
            except Exception:
                state = None
        return job, state


# ── 1. Loop breaker ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stranded_resets_count_up_then_fail_with_single_refund(
    tmp_path, monkeypatch
):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 930101
        async with maker() as s:
            s.add(User(id=uid, recap_credits=4))  # submit already took 1 of 5
            s.add(_job("rc_ab_loop", uid, _recap_state()))
            await s.commit()

        # 1st rescue: re-queued, count persisted as 1, no refund.
        assert await reset_stale_running_recap_jobs(cutoff_minutes=10) == 1
        job, state = await _stored(maker, "rc_ab_loop")
        assert job.status == JobStatus.QUEUED.value
        assert job.started_at is None
        assert state["stranded_reset_count"] == 1

        # A later, separate sweep invocation (fresh sessions) sees the
        # persisted count and bumps it to 2 — still re-queued.
        await _restrand(maker, "rc_ab_loop")
        assert await reset_stale_running_recap_jobs(cutoff_minutes=10) == 1
        job, state = await _stored(maker, "rc_ab_loop")
        assert job.status == JobStatus.QUEUED.value
        assert state["stranded_reset_count"] == 2

        # Bulky media of the doomed job is stripped by the fail path.
        job_dir = settings.storage_root / "recap_output" / "rc_ab_loop"
        job_dir.mkdir(parents=True)
        (job_dir / "source.mp4").write_bytes(b"x" * 2048)
        (job_dir / "checkpoint.json").write_text("{}")

        # 3rd rescue hits the limit: FAILED terminally, NOT re-queued,
        # refunded exactly once, counter cleared by the terminal write.
        await _restrand(maker, "rc_ab_loop")
        assert await reset_stale_running_recap_jobs(cutoff_minutes=10) == 1
        job, state = await _stored(maker, "rc_ab_loop")
        assert job.status == JobStatus.FAILED.value
        assert job.error_message == api_mod._STRANDED_RESET_FAILURE_MESSAGE
        assert job.finished_at is not None
        assert state["stranded_reset_count"] == 0
        assert state["credit_refunded"] is True
        assert state["credits_refunded"] == 1
        assert not (job_dir / "source.mp4").exists()
        assert (job_dir / "checkpoint.json").exists()
        async with maker() as s:
            user = await s.get(User, uid)
            assert user.recap_credits == 5  # exactly whole again
            refunds = (
                await s.execute(
                    select(RecapCreditLedgerEvent).where(
                        RecapCreditLedgerEvent.reference_id == "REFUND:rc_ab_loop"
                    )
                )
            ).scalars().all()
            assert len(refunds) == 1
            assert refunds[0].direction == "CREDIT"
            assert refunds[0].credits == 1

        # Terminal rows are never touched again — and never refunded twice.
        await _restrand(maker, "rc_ab_loop")  # even a manual flip back…
        async with maker() as s:  # …is undone here: restore FAILED first
            job = await s.get(Job, "rc_ab_loop")
            job.status = JobStatus.FAILED.value
            await s.commit()
        assert await reset_stale_running_recap_jobs(cutoff_minutes=10) == 0
        async with maker() as s:
            user = await s.get(User, uid)
            assert user.recap_credits == 5
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_both_sweep_modes_share_the_counting_behaviour(
    tmp_path, monkeypatch
):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 930102
        async with maker() as s:
            s.add(User(id=uid, recap_credits=4))
            s.add(_job("rc_ab_rel", uid, _recap_state(), minutes_ago=30))
            s.add(_job("rc_ab_abs", uid, _recap_state(), minutes_ago=2))
            await s.commit()

        # Relative (startup) mode: only the 30-minute-old row is stranded.
        assert await reset_stale_running_recap_jobs(cutoff_minutes=10) == 1
        job, state = await _stored(maker, "rc_ab_rel")
        assert job.status == JobStatus.QUEUED.value
        assert state["stranded_reset_count"] == 1
        job, _ = await _stored(maker, "rc_ab_abs")
        assert job.status == JobStatus.RUNNING.value

        # Absolute (periodic, boot-time) mode: the 2-minute-old row is
        # stranded relative to a boot that happened 1 minute ago, and
        # the SAME counting applies.
        assert await reset_stale_running_recap_jobs(
            absolute_cutoff=utc_now() - timedelta(minutes=1)
        ) == 1
        job, state = await _stored(maker, "rc_ab_abs")
        assert job.status == JobStatus.QUEUED.value
        assert state["stranded_reset_count"] == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_row_without_blob_requeues_without_counting(
    tmp_path, monkeypatch
):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 930103
        async with maker() as s:
            s.add(User(id=uid, recap_credits=4))
            s.add(_job("rc_ab_legacy", uid, None))
            bad = _job("rc_ab_badblob", uid, None)
            bad.recap_config = "not-json{"
            s.add(bad)
            await s.commit()

        assert await reset_stale_running_recap_jobs(cutoff_minutes=10) == 2
        for jid in ("rc_ab_legacy", "rc_ab_badblob"):
            job, _ = await _stored(maker, jid)
            assert job.status == JobStatus.QUEUED.value
            assert job.started_at is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_clear_helper_zeroes_a_stored_count(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 930104
        async with maker() as s:
            s.add(User(id=uid, recap_credits=4))
            s.add(
                _job(
                    "rc_ab_clear",
                    uid,
                    _recap_state(stranded_reset_count=2),
                    status=JobStatus.FAILED.value,
                )
            )
            await s.commit()

        await api_mod._clear_recap_stranded_reset_count("rc_ab_clear")
        _, state = await _stored(maker, "rc_ab_clear")
        assert state["stranded_reset_count"] == 0
        # Idempotent / quiet on a job whose count is already zero.
        await api_mod._clear_recap_stranded_reset_count("rc_ab_clear")
        _, state = await _stored(maker, "rc_ab_clear")
        assert state["stranded_reset_count"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_admin_retry_reactivation_clears_the_count(
    tmp_path, monkeypatch
):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 930105
        async with maker() as s:
            s.add(User(id=uid, recap_credits=5))
            s.add(
                _job(
                    "rc_ab_retry",
                    uid,
                    _recap_state(
                        stranded_reset_count=2,
                        credit_refunded=True,
                        credits_refunded=1,
                    ),
                    status=JobStatus.FAILED.value,
                )
            )
            await s.commit()

        async def _fake_run(*args, **kwargs):
            return None

        monkeypatch.setattr(api_mod, "_run_recap_background_job", _fake_run)
        admin = User(id=424242)
        async with maker() as s:
            result = await api_mod.retry_queued_job(
                "rc_ab_retry", admin=admin, db=s
            )
        assert result["success"] is True
        job, state = await _stored(maker, "rc_ab_retry")
        assert job.status == JobStatus.QUEUED.value
        assert state["stranded_reset_count"] == 0
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop("rc_ab_retry", None)
        await engine.dispose()


# ── 2. NVENC probe ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_probe_cache():
    core._NVENC_PROBE_CACHE.clear()
    yield
    core._NVENC_PROBE_CACHE.clear()


def _fake_completed(cmd, returncode=0):
    return subprocess.CompletedProcess(cmd, returncode, "", "probe err")


def test_nvenc_probe_result_is_cached(monkeypatch):
    calls = []

    def fake_run(cmd, *, timeout, what):
        calls.append(list(cmd))
        return _fake_completed(cmd, 0)

    monkeypatch.setattr(core, "_run_media", fake_run)
    assert core.nvenc_usable("ffmpeg-x") is True
    assert core.nvenc_usable("ffmpeg-x") is True
    assert len(calls) == 1  # one probe per process per ffmpeg binary
    assert "h264_nvenc" in calls[0]
    assert calls[0][calls[0].index("-frames:v") + 1] == "1"


def test_nvenc_probe_failure_and_exception_mean_unusable(monkeypatch):
    monkeypatch.setattr(
        core, "_run_media", lambda cmd, *, timeout, what: _fake_completed(cmd, 1)
    )
    assert core.nvenc_usable("ffmpeg-bad") is False

    def boom(cmd, *, timeout, what):
        raise RuntimeError("timed out")

    monkeypatch.setattr(core, "_run_media", boom)
    assert core.nvenc_usable("ffmpeg-slow") is False


def test_encoder_selection_probe_fail_forces_libx264(monkeypatch):
    monkeypatch.setattr(
        core, "ffmpeg_encoders", lambda ff: {"h264_nvenc", "libx264"}
    )
    monkeypatch.setattr(core, "nvenc_usable", lambda ff: False)
    # Even a checkpoint that remembers h264_nvenc must not resurrect it.
    assert (
        core._select_render_encoder("ffmpeg", {"selected_encoder": "h264_nvenc"})
        == "libx264"
    )
    assert core._select_render_encoder("ffmpeg", {}) == "libx264"


def test_encoder_selection_probe_ok_keeps_remembered_logic(monkeypatch):
    monkeypatch.setattr(
        core, "ffmpeg_encoders", lambda ff: {"h264_nvenc", "libx264"}
    )
    monkeypatch.setattr(core, "nvenc_usable", lambda ff: True)
    assert core._select_render_encoder("ffmpeg", {}) == "h264_nvenc"
    assert (
        core._select_render_encoder("ffmpeg", {"selected_encoder": "h264_nvenc"})
        == "h264_nvenc"
    )
    assert (
        core._select_render_encoder("ffmpeg", {"selected_encoder": "libx264"})
        == "libx264"
    )


def test_encoder_selection_unlisted_nvenc_is_libx264(monkeypatch):
    monkeypatch.setattr(core, "ffmpeg_encoders", lambda ff: {"libx264"})
    monkeypatch.setattr(core, "nvenc_usable", lambda ff: True)
    assert core._select_render_encoder("ffmpeg", {}) == "libx264"


# ── 3. Overlay two-step split ────────────────────────────────────────

def _capture_media(monkeypatch, *, fail_nvenc=False, fail_all=False):
    calls = []

    def fake_run(cmd, *, timeout, what):
        calls.append(list(cmd))
        if fail_all or (fail_nvenc and "h264_nvenc" in cmd):
            return subprocess.CompletedProcess(cmd, 1, "", "encode boom")
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x" * 6000)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(core, "_run_media", fake_run)
    return calls


def _graph_of(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


def _effective_threads(cmd):
    idxs = [i for i, a in enumerate(cmd) if a == "-threads"]
    assert idxs, cmd
    return cmd[idxs[-1] + 1]


_OVERLAYS_FULL = {
    # Batch DD: no title_text — the feature is removed; cues +
    # blur boxes activate the pass on their own.
    "subtitle_cues": [[0.5, 2.5, "မြို့ကြီးဆီကို ထွက်ခွာပြီး"]],
    "subtitles_font_size": 16,
    "sub_style": "yellow",
    "sub_lines": 1,
    "sub_y": 0.85,
    "blur_boxes": [
        {"x": 0.05, "y": 0.85, "w": 0.90, "h": 0.16},
        {"x": 0.10, "y": 0.30, "w": 0.50, "h": 0.10},
    ],
    "blur_strength": 18,
}


def test_overlay_with_blur_runs_two_steps(tmp_path, monkeypatch):
    calls = _capture_media(monkeypatch)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"0" * 100)
    out = tmp_path / "out.mp4"
    work = tmp_path / "w"
    result = core.apply_production_overlays(
        "ffmpeg", src, out, dict(_OVERLAYS_FULL), "libx264", work,
        frame_size=(320, 240), layout={"pw": 320, "ph": 240, "ox": 0, "oy": 0},
        duration=4,
    )
    assert result == out
    assert len(calls) == 2
    step1, step2 = calls

    # Step 1: blur only — one input, boxblur graph, no title/cue overlays,
    # scratch intermediate output, no +faststart.
    assert step1.count("-i") == 1
    g1 = _graph_of(step1)
    assert "boxblur" in g1
    assert "vtitle" not in g1 and "vsub" not in g1 and "between(t," not in g1
    intermediate = work / "overlay_step1_blurred.mp4"
    assert step1[-1] == str(intermediate)
    assert "+faststart" not in step1

    # Step 2: everything else over the intermediate — no boxblur, cue
    # enable expressions present, decorated output with +faststart.
    assert step2[step2.index("-i") + 1] == str(intermediate)
    g2 = _graph_of(step2)
    assert "boxblur" not in g2
    assert "between(t,0.500,2.500)" in g2
    assert "vtitle" not in g2  # Batch DD: title overlay removed
    assert step2[-1] == str(out)
    assert "+faststart" in step2

    # Both steps carry the thread cap as the EFFECTIVE (last) -threads.
    assert _effective_threads(step1) == "2"
    assert _effective_threads(step2) == "2"

    # The scratch intermediate is removed after success.
    assert not intermediate.exists()


def test_overlay_without_blur_runs_one_step(tmp_path, monkeypatch):
    calls = _capture_media(monkeypatch)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"0" * 100)
    out = tmp_path / "out.mp4"
    result = core.apply_production_overlays(
        # Batch DD: subtitle cues activate the one-step pass (the
        # old title_text activator no longer exists).
        "ffmpeg", src, out, {"subtitle_cues": [[0.5, 2.5, "နမူနာ စာတန်း"]], "subtitles_font_size": 16, "sub_style": "yellow", "sub_lines": 1, "sub_y": 0.85}, "libx264",
        tmp_path / "w", frame_size=(320, 240),
        layout={"pw": 320, "ph": 240, "ox": 0, "oy": 0}, duration=4,
    )
    assert result == out
    assert len(calls) == 1
    assert "boxblur" not in _graph_of(calls[0])
    assert calls[0][-1] == str(out)
    assert "+faststart" in calls[0]
    assert _effective_threads(calls[0]) == "2"


def test_overlay_noop_guard_unchanged(tmp_path, monkeypatch):
    calls = _capture_media(monkeypatch)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"0" * 100)
    result = core.apply_production_overlays(
        "ffmpeg", src, tmp_path / "out.mp4", {}, "libx264", tmp_path / "w",
        frame_size=(320, 240), layout={"pw": 320, "ph": 240, "ox": 0, "oy": 0},
        duration=4,
    )
    assert result == src
    assert calls == []


def test_overlay_nvenc_failure_falls_back_per_step(tmp_path, monkeypatch):
    calls = _capture_media(monkeypatch, fail_nvenc=True)
    logs = []
    src = tmp_path / "src.mp4"
    src.write_bytes(b"0" * 100)
    out = tmp_path / "out.mp4"
    work = tmp_path / "w"
    result = core.apply_production_overlays(
        "ffmpeg", src, out, dict(_OVERLAYS_FULL), "h264_nvenc", work,
        log=logs.append,
        frame_size=(320, 240), layout={"pw": 320, "ph": 240, "ox": 0, "oy": 0},
        duration=4,
    )
    assert result == out
    # Step 1: nvenc attempt + libx264 rebuild; step 2 then uses libx264
    # directly (no second doomed nvenc attempt).
    assert len(calls) == 3
    assert "h264_nvenc" in calls[0]
    assert "libx264" in calls[1] and "h264_nvenc" not in calls[1]
    assert "libx264" in calls[2] and "h264_nvenc" not in calls[2]
    assert "⚠️ Overlay NVENC failed — libx264 fallback" in logs
    assert _effective_threads(calls[1]) == "2"
    assert not (work / "overlay_step1_blurred.mp4").exists()


def test_overlay_total_failure_raises(tmp_path, monkeypatch):
    _capture_media(monkeypatch, fail_all=True)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"0" * 100)
    with pytest.raises(RuntimeError, match="Production overlay render failed"):
        core.apply_production_overlays(
            # Batch DD: subtitle cues activate the pass (title_text
            # alone is inert now and would return the input).
            "ffmpeg", src, tmp_path / "out.mp4", {"subtitle_cues": [[0.5, 2.5, "နမူနာ စာတန်း"]], "subtitles_font_size": 16, "sub_style": "yellow", "sub_lines": 1, "sub_y": 0.85},
            "libx264", tmp_path / "w", frame_size=(320, 240),
            layout={"pw": 320, "ph": 240, "ox": 0, "oy": 0}, duration=4,
        )
