"""Batch CC (2026-11-07): memory-safe production overlay.

Incident: a 24-segment ~9.2 min drama recap (312 subtitle cues,
1080×1920) finished every stage through SYNC and died in the final
Production overlay pass — the full-graph step peaked at 3,306 MB on
the 4 GB instance, the process OOM-crash-looped through recovery,
and the job failed after ~67 min of wall clock. Three fixes:

1. CHUNKED CUE OVERLAY — a HEAVY job (cue count > 120 OR duration
   > 240 s) never enters the full-graph step 2: the cue/title step
   runs in ≤ 60 s windows carrying only the cues that intersect each
   window (title/logo/extra/pattern in every window, exactly as the
   single graph applies them full-duration), then stream-copy
   concats the windows. Encoder settings per window are the step-2
   settings verbatim. Non-heavy jobs keep the classic paths.
2. GLOBAL OVERLAY SLOT — the old single-pass-only lock now guards
   EVERY production overlay encode in apply_production_overlays, so
   two jobs' passes can no longer sum their memory.
3. STAGE-AWARE RECOVERY CAP — Batch AB's stranded-reset counter
   becomes per-stage: the run's progress seams keep a furthest-stage
   witness file; a rescue whose job had advanced past the stage
   recorded at the previous rescue restarts the count for the new
   stage. Same-stage 3rd rescue still fails the job with exactly one
   refund (AB machinery, unchanged).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
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


# ── 1a. Heavy classifier + chunk planner (pure) ─────────────────────

def test_heavy_classifier_thresholds():
    assert core._overlay_is_heavy(121, None) is True
    assert core._overlay_is_heavy(300, 100.0) is True
    assert core._overlay_is_heavy(120, 240.1) is True
    assert core._overlay_is_heavy(0, 570.0) is True
    assert core._overlay_is_heavy(120, 240.0) is False
    assert core._overlay_is_heavy(119, 100.0) is False
    assert core._overlay_is_heavy(0, None) is False
    assert core._overlay_is_heavy(10, None) is False


def test_chunk_planner_assigns_intersecting_cues():
    cues = [(0.0, 10.0), (59.0, 61.0), (60.0, 62.0), (119.0, 121.0)]
    windows = core._plan_overlay_chunks(cues, 130.0)
    assert windows == [
        (0.0, 60.0, [0, 1]),
        (60.0, 120.0, [1, 2, 3]),
        (120.0, 130.0, [3]),
    ]


def test_chunk_planner_no_cues_and_unknown_duration():
    # Duration-driven heaviness with no cues: bare windows, no crash.
    windows = core._plan_overlay_chunks([], 250.0)
    assert [w[:2] for w in windows] == [
        (0.0, 60.0), (60.0, 120.0), (120.0, 180.0), (180.0, 240.0), (240.0, 250.0),
    ]
    assert all(w[2] == [] for w in windows)
    # Unknown duration falls back to the last cue end.
    windows = core._plan_overlay_chunks([(10.0, 20.0), (100.0, 125.0)], None)
    assert windows[-1][1] == 125.0
    assert windows[0][2] == [0]
    assert windows[1][2] == [1]


# ── 1b. Chunked command construction (captured ffmpeg calls) ───────

def _capture_media(monkeypatch, *, fail_nvenc=False):
    calls = []
    concat_lists = []

    def fake_run(cmd, *, timeout, what):
        calls.append(list(cmd))
        if "concat" in cmd:
            list_path = Path(cmd[cmd.index("-i") + 1])
            concat_lists.append(list_path.read_text(encoding="utf-8"))
        if fail_nvenc and "h264_nvenc" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "encode boom")
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x" * 6000)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(core, "_run_media", fake_run)
    return calls, concat_lists


def _graph_of(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


def _effective_threads(cmd):
    idxs = [i for i, a in enumerate(cmd) if a == "-threads"]
    assert idxs, cmd
    return cmd[idxs[-1] + 1]


def _cues_overlays(count, duration, **over):
    step = (duration - 10.0) / count
    cues = [
        [round(2.0 + i * step, 3), round(2.0 + i * step + 1.0, 3), f"Cue {i + 1} စာသား"]
        for i in range(count)
    ]
    overlays = {
        "subtitle_cues": cues,
        "subtitles_font_size": 16,
        "sub_style": "yellow",
        "sub_lines": 1,
        "sub_y": 0.85,
    }
    overlays.update(over)
    return overlays


def test_heavy_job_with_blur_chunks_instead_of_full_graph(tmp_path, monkeypatch):
    calls, concat_lists = _capture_media(monkeypatch)
    probe_calls = []
    monkeypatch.setattr(
        core, "_run_overlay_probe",
        lambda cmd, timeout=180.0: probe_calls.append(list(cmd)) or (0, 1, ""),
    )
    src = tmp_path / "src.mp4"
    src.write_bytes(b"0" * 100)
    out = tmp_path / "out.mp4"
    work = tmp_path / "w"
    overlays = _cues_overlays(
        130, 300.0,
        blur_boxes=[{"x": 0.05, "y": 0.85, "w": 0.90, "h": 0.16}],
        blur_strength=18,
    )
    result = core.apply_production_overlays(
        "ffmpeg", src, out, overlays, "libx264", work,
        frame_size=(320, 240), layout={"pw": 320, "ph": 240, "ox": 0, "oy": 0},
        duration=300.0,
    )
    assert result == out
    # Heavy jobs never probe / never single-pass: the probe command IS
    # the full graph (the incident probe itself peaked at 3.3 GB).
    assert probe_calls == []
    # step1 (blur) + 5 chunks (300 s / 60 s) + 1 concat.
    assert len(calls) == 7
    step1 = calls[0]
    assert "boxblur" in _graph_of(step1)
    intermediate = work / "overlay_step1_blurred.mp4"
    assert step1[-1] == str(intermediate)

    chunks = calls[1:6]
    expected_args = core.encoder_args_for_quality("libx264", "standard")
    for k, cmd in enumerate(chunks):
        # Windowed input seeking BEFORE the first -i, off the blurred
        # intermediate, carrying only this window's cues.
        assert cmd[cmd.index("-ss") + 1] == f"{k * 60:.3f}"
        assert cmd.index("-ss") < cmd.index("-i")
        assert cmd[cmd.index("-t") + 1] == "60.000"
        assert cmd[cmd.index("-i") + 1] == str(intermediate)
        # Encoder settings identical to the classic step 2.
        at = cmd.index(expected_args[0])
        assert cmd[at:at + len(expected_args)] == expected_args
        assert _effective_threads(cmd) == "2"
        assert "+faststart" not in cmd  # intermediates, like step 1
        assert cmd[-1] == str(work / f"overlay_chunk_{k:03d}.mp4")
    # No call ever carries the full 130-cue graph.
    for cmd in calls:
        assert cmd.count("-i") < 40
    # Cues are time-shifted into their window: cue 53 runs
    # 120.231–121.231, so in window 2 (120–180) it enables at 0.231;
    # cue 52 (118–119) sits in window 1 at 58–59.
    assert "between(t,0.231,1.231)" in _graph_of(chunks[2])
    assert "between(t,58.000,59.000)" in _graph_of(chunks[1])

    concat = calls[6]
    assert concat[concat.index("-c") + 1] == "copy"
    assert "+faststart" in concat
    assert concat[-1] == str(out)
    # Concat list order == window order, entries absolute (the
    # demuxer resolves relative entries against the list's own dir).
    listed = [
        line.split("'")[1] for line in concat_lists[0].strip().splitlines()
    ]
    assert listed == [str(work / f"overlay_chunk_{k:03d}.mp4") for k in range(5)]
    assert all(Path(p).is_absolute() for p in listed)
    # Chunk files are cleaned up after a successful concat.
    assert not list(work.glob("overlay_chunk_*.mp4"))
    assert not intermediate.exists()


def test_heavy_job_without_blur_chunks_off_input(tmp_path, monkeypatch):
    calls, _ = _capture_media(monkeypatch)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"0" * 100)
    out = tmp_path / "out.mp4"
    work = tmp_path / "w"
    overlays = _cues_overlays(130, 300.0)
    result = core.apply_production_overlays(
        "ffmpeg", src, out, overlays, "libx264", work,
        frame_size=(320, 240), layout={"pw": 320, "ph": 240, "ox": 0, "oy": 0},
        duration=300.0,
    )
    assert result == out
    # No blur: no step 1 — 5 chunks straight off the source + concat.
    assert len(calls) == 6
    for cmd in calls[:5]:
        assert "boxblur" not in _graph_of(cmd)
        assert cmd[cmd.index("-i") + 1] == str(src)


def test_chunked_concat_list_absolute_with_relative_work_dir(tmp_path, monkeypatch):
    # Regression: with a RELATIVE work dir the chunk paths in the
    # concat list must still be absolute — the demuxer resolves
    # relative entries against the list file's directory.
    calls, concat_lists = _capture_media(monkeypatch)
    monkeypatch.chdir(tmp_path)
    src = Path("src.mp4")
    src.write_bytes(b"0" * 100)
    overlays = _cues_overlays(130, 300.0)
    result = core.apply_production_overlays(
        "ffmpeg", src, Path("out.mp4"), overlays, "libx264", Path("w"),
        frame_size=(320, 240), layout={"pw": 320, "ph": 240, "ox": 0, "oy": 0},
        duration=300.0,
    )
    assert result == Path("out.mp4")
    listed = [
        line.split("'")[1] for line in concat_lists[0].strip().splitlines()
    ]
    assert listed and all(Path(p).is_absolute() for p in listed)


def test_heavy_by_duration_only_still_chunks(tmp_path, monkeypatch):
    calls, _ = _capture_media(monkeypatch)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"0" * 100)
    out = tmp_path / "out.mp4"
    overlays = _cues_overlays(10, 300.0, title_text="Hello Title")
    result = core.apply_production_overlays(
        "ffmpeg", src, out, overlays, "libx264", tmp_path / "w",
        frame_size=(320, 240), layout={"pw": 320, "ph": 240, "ox": 0, "oy": 0},
        duration=300.0,
    )
    assert result == out
    assert len(calls) == 6  # 5 chunks + concat, several cue-less
    # Batch DD: the legacy title_text in the overlays dict is inert
    # — no chunk graph carries a title step any more.
    for cmd in calls[:5]:
        assert "vtitle" not in _graph_of(cmd)


def test_just_under_threshold_keeps_classic_two_step(tmp_path, monkeypatch):
    calls, _ = _capture_media(monkeypatch)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"0" * 100)
    out = tmp_path / "out.mp4"
    work = tmp_path / "w"
    overlays = _cues_overlays(
        120, 240.0,
        blur_boxes=[{"x": 0.05, "y": 0.85, "w": 0.90, "h": 0.16}],
    )
    result = core.apply_production_overlays(
        "ffmpeg", src, out, overlays, "libx264", work,
        frame_size=(320, 240), layout={"pw": 320, "ph": 240, "ox": 0, "oy": 0},
        duration=240.0,
    )
    assert result == out
    # Classic path: the (real) probe fails on the fake source, then
    # step 1 + step 2 exactly as Batch AB pinned — no -ss anywhere.
    assert len(calls) == 2
    for cmd in calls:
        assert "-ss" not in cmd
    assert calls[1].count("-i") == 1 + 120  # full cue graph, unchanged


# ── 2. Global overlay slot ──────────────────────────────────────────

def test_overlay_slot_blocks_a_second_job(tmp_path, monkeypatch):
    entered = threading.Event()

    def fake_run(cmd, *, timeout, what):
        entered.set()
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x" * 6000)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(core, "_run_media", fake_run)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"0" * 100)
    done = {}

    def _job():
        done["result"] = core.apply_production_overlays(
            # Batch DD: a blur-box list activates the overlay pass
            # (the old title_text activator no longer exists).
            "ffmpeg", src, tmp_path / "out.mp4",
            {"blur_boxes": [{"x": 0.1, "y": 0.5, "w": 0.5, "h": 0.1, "strength": 18}]},
            "libx264", tmp_path / "w", frame_size=(320, 240),
            layout={"pw": 320, "ph": 240, "ox": 0, "oy": 0}, duration=4,
        )

    core._OVERLAY_SINGLE_PASS_SLOT.acquire()
    try:
        th = threading.Thread(target=_job)
        th.start()
        # While the slot is held, the job's encode must not even start.
        assert not entered.wait(timeout=0.7)
        assert th.is_alive()
    finally:
        core._OVERLAY_SINGLE_PASS_SLOT.release()
    assert entered.wait(timeout=10)
    th.join(timeout=10)
    assert done["result"] == tmp_path / "out.mp4"


# ── 1c. Behavioural: real ffmpeg, small scale ──────────────────────

ffmpeg_missing = shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None


def _make_clip(path: Path, seconds: int, size="320x180"):
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"testsrc2=size={size}:rate=25:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, timeout=300,
    )


def _probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    return float(out.stdout.strip())


@pytest.mark.skipif(ffmpeg_missing, reason="ffmpeg/ffprobe not available")
def test_chunked_render_small_scale_boundary_cue(tmp_path):
    src = tmp_path / "src.mp4"
    # Moving testsrc2 in the top 70%, a solid dark band at the
    # bottom: the subtitle band's background is static, so the
    # yellow-cue pixel count below is a deterministic signal (the
    # full-motion source measured a noisy ±600 baseline).
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc2=size=320x180:rate=25:duration=130",
         "-vf", "crop=320:126:0:0,pad=320:180:0:0:color=0x181818",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
         "-pix_fmt", "yuv420p", str(src)],
        check=True, timeout=300,
    )
    # Dense cues everywhere EXCEPT a clean gap around t=30, plus one
    # cue spanning the 60 s window boundary (59.2 → 60.8).
    cues = []
    t = 2.0
    while t < 128.0:
        if not (29.0 < t < 31.5) and not (58.0 < t < 62.0):
            cues.append([round(t, 2), round(t + 0.8, 2), "နမူနာ စာသား တစ်ကြောင်း"])
        t += 1.0
    cues.append([59.2, 60.8, "နယ်စပ် ကျော်ဖြတ် စာသား"])
    assert len(cues) > core.OVERLAY_HEAVY_CUE_COUNT  # heavy by count
    overlays = {
        "blur_boxes": [{"x": 0.05, "y": 0.85, "w": 0.90, "h": 0.16}],
        "blur_strength": 18,
        "subtitle_cues": cues,
        "subtitles_font_size": 16,
        "sub_style": "yellow",
        "sub_lines": 1,
        "sub_y": 0.85,
    }
    out = tmp_path / "out.mp4"
    result = core.apply_production_overlays(
        "ffmpeg", src, out, overlays, "libx264", tmp_path / "w",
        frame_size=(320, 180), layout={"pw": 320, "ph": 180, "ox": 0, "oy": 0},
        render_quality="standard", duration=130.0,
    )
    assert result == out
    # Duration preserved across the window concat (±0.5 s).
    assert abs(_probe_duration(out) - 130.0) <= 0.5
    # The overlay product is video-only, exactly like the classic
    # step 2 (audio is muxed later by the pipeline, never carried
    # through this function — both paths use -an).
    streams = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(out)],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout.split()
    assert streams == ["video"]

    # Boundary cue: extract frames inside the spanning cue on BOTH
    # sides of the 60 s seam and in the clean gap. The source moves,
    # so instead of a frame diff count saturated-yellow pixels in
    # the subtitle band: the yellow-style cue text produces them by
    # the thousand; the blurred background-only gap frame does not.
    from PIL import Image

    def band_yellow(ts: float) -> int:
        frame = tmp_path / f"frame_{ts}.png"
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", str(ts), "-i", str(out), "-frames:v", "1", str(frame)],
            check=True, timeout=60,
        )
        img = Image.open(frame).convert("RGB")
        w, h = img.size
        px = img.crop((0, int(h * 0.72), w, int(h * 0.99))).getdata()
        return sum(1 for r, g, b in px if r > 150 and g > 150 and b < 130)

    gap = band_yellow(30.0)
    before = band_yellow(59.6)
    after = band_yellow(60.4)
    assert before > gap + 300  # cue visible before the seam
    assert after > gap + 300   # …and after the seam (next window)


@pytest.mark.skipif(ffmpeg_missing, reason="ffmpeg/ffprobe not available")
def test_chunked_vs_classic_ssim_spot_check(tmp_path):
    src = tmp_path / "src.mp4"
    _make_clip(src, 130)
    cues = [
        [round(2.0 + i * 0.95, 2), round(2.0 + i * 0.95 + 0.7, 2), "အရည်အသွေး စမ်းသပ် စာသား"]
        for i in range(130)
    ]
    overlays = {
        "blur_boxes": [{"x": 0.05, "y": 0.85, "w": 0.90, "h": 0.16}],
        "blur_strength": 18,
        "subtitle_cues": cues,
        "subtitles_font_size": 16,
        "sub_style": "yellow",
        "sub_lines": 1,
        "sub_y": 0.85,
    }
    common = dict(
        frame_size=(320, 180), layout={"pw": 320, "ph": 180, "ox": 0, "oy": 0},
        render_quality="standard", duration=130.0,
    )
    out_chunked = tmp_path / "chunked.mp4"
    core.apply_production_overlays(
        "ffmpeg", src, out_chunked, dict(overlays), "libx264",
        tmp_path / "wc", **common,
    )
    # Classic path on the same input: thresholds lifted so the same
    # job is no longer "heavy" (probe fails over to two-step on this
    # host or single-passes — either is the classic full graph).
    old_cue = core.OVERLAY_HEAVY_CUE_COUNT
    old_dur = core.OVERLAY_HEAVY_DURATION_SECONDS
    core.OVERLAY_HEAVY_CUE_COUNT = 100000
    core.OVERLAY_HEAVY_DURATION_SECONDS = 100000.0
    try:
        out_classic = tmp_path / "classic.mp4"
        core.apply_production_overlays(
            "ffmpeg", src, out_classic, dict(overlays), "libx264",
            tmp_path / "wk", **common,
        )
    finally:
        core.OVERLAY_HEAVY_CUE_COUNT = old_cue
        core.OVERLAY_HEAVY_DURATION_SECONDS = old_dur
    assert abs(_probe_duration(out_chunked) - _probe_duration(out_classic)) <= 0.5
    ssim = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(out_chunked), "-i", str(out_classic),
         "-lavfi", "ssim", "-f", "null", "-"],
        capture_output=True, text=True, timeout=300,
    )
    tail = (ssim.stderr or "") + (ssim.stdout or "")
    all_line = [ln for ln in tail.splitlines() if " All:" in ln]
    assert all_line, tail[-500:]
    score = float(all_line[-1].split("All:")[1].split()[0])
    assert score >= 0.98


# ── 3. Stage-aware stranded recovery (Batch AB harness) ────────────

async def _open_store(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/batch_cc.db", future=True
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


def _write_witness(job_id, stage):
    job_dir = settings.storage_root / "recap_output" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "progress_stage.json").write_text(
        json.dumps({"stage": stage}), encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_stage_aware_counting_reset_and_fail(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 940101
        async with maker() as s:
            s.add(User(id=uid, recap_credits=4))
            s.add(_job("rc_cc_stage", uid, _recap_state()))
            await s.commit()

        # Rescue 1: fresh job re-dispatches (count 0 → 1), stage
        # recorded from the witness.
        _write_witness("rc_cc_stage", "sync")
        assert await reset_stale_running_recap_jobs(cutoff_minutes=10) == 1
        job, state = await _stored(maker, "rc_cc_stage")
        assert job.status == JobStatus.QUEUED.value
        assert state["stranded_reset_count"] == 1
        assert state["stranded_stage"] == "sync"

        # Rescue 2: died at the SAME stage — count marches to 2.
        await _restrand(maker, "rc_cc_stage")
        assert await reset_stale_running_recap_jobs(cutoff_minutes=10) == 1
        job, state = await _stored(maker, "rc_cc_stage")
        assert job.status == JobStatus.QUEUED.value
        assert state["stranded_reset_count"] == 2
        assert state["stranded_stage"] == "sync"

        # Rescue 3: but the job had ADVANCED to finalize before dying
        # — progress, not a loop: the count restarts at 1 for the new
        # stage and the job is re-queued, NOT failed, no refund.
        await _restrand(maker, "rc_cc_stage")
        _write_witness("rc_cc_stage", "finalize")
        assert await reset_stale_running_recap_jobs(cutoff_minutes=10) == 1
        job, state = await _stored(maker, "rc_cc_stage")
        assert job.status == JobStatus.QUEUED.value
        assert state["stranded_reset_count"] == 1
        assert state["stranded_stage"] == "finalize"
        async with maker() as s:
            user = await s.get(User, uid)
            assert user.recap_credits == 4

        # Rescues 4–5 at finalize: count 2, then the cap fails the
        # job with exactly one refund (Batch AB machinery).
        await _restrand(maker, "rc_cc_stage")
        assert await reset_stale_running_recap_jobs(cutoff_minutes=10) == 1
        job, state = await _stored(maker, "rc_cc_stage")
        assert state["stranded_reset_count"] == 2

        await _restrand(maker, "rc_cc_stage")
        assert await reset_stale_running_recap_jobs(cutoff_minutes=10) == 1
        job, state = await _stored(maker, "rc_cc_stage")
        assert job.status == JobStatus.FAILED.value
        assert state["stranded_reset_count"] == 0
        assert state["stranded_stage"] is None
        assert state["credit_refunded"] is True
        async with maker() as s:
            user = await s.get(User, uid)
            assert user.recap_credits == 5  # exactly whole again
            refunds = (
                await s.execute(
                    select(RecapCreditLedgerEvent).where(
                        RecapCreditLedgerEvent.reference_id == "REFUND:rc_cc_stage"
                    )
                )
            ).scalars().all()
            assert len(refunds) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fresh_job_without_witness_keeps_ab_counting(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 940102
        async with maker() as s:
            s.add(User(id=uid, recap_credits=4))
            s.add(_job("rc_cc_plain", uid, _recap_state()))
            await s.commit()
        # No witness file anywhere: Batch AB behaviour verbatim.
        assert await reset_stale_running_recap_jobs(cutoff_minutes=10) == 1
        job, state = await _stored(maker, "rc_cc_plain")
        assert job.status == JobStatus.QUEUED.value
        assert state["stranded_reset_count"] == 1
        assert not state.get("stranded_stage")
    finally:
        await engine.dispose()


# ── Release pin: cache token r101 ───────────────────────────────────

def test_cache_token_is_r101():
    from app.webapp import api as _api_mod

    index_html = (
        Path(_api_mod.__file__).resolve().parent / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert index_html.count("20261116_r156") == 4
    assert ("20261104_r" + "100") not in index_html


def test_witness_note_helper_is_monotone_and_best_effort(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    job_dir = settings.storage_root / "recap_output" / "rc_cc_note"
    job_dir.mkdir(parents=True)
    entry = {"id": "rc_cc_note"}
    try:
        api_mod._note_recap_progress_stage(entry, "sync")
        assert api_mod._read_recap_stage_witness("rc_cc_note") == "sync"
        # A retry's earlier stage never moves the witness backwards.
        api_mod._note_recap_progress_stage(entry, "tts")
        assert api_mod._read_recap_stage_witness("rc_cc_note") == "sync"
        # Advancing works.
        api_mod._note_recap_progress_stage(entry, "finalize")
        assert api_mod._read_recap_stage_witness("rc_cc_note") == "finalize"
        # Unknown stages / missing dirs are quiet no-ops.
        api_mod._note_recap_progress_stage(entry, "bogus")
        api_mod._note_recap_progress_stage({"id": "rc_cc_ghost"}, "sync")
        assert api_mod._read_recap_stage_witness("rc_cc_note") == "finalize"
        assert api_mod._read_recap_stage_witness("rc_cc_ghost") is None
        api_mod._note_recap_progress_stage({}, "sync")
        api_mod._note_recap_progress_stage(entry, None)
    finally:
        api_mod._STAGE_WITNESS_RANKS.pop("rc_cc_note", None)
        api_mod._STAGE_WITNESS_RANKS.pop("rc_cc_ghost", None)


@pytest.mark.asyncio
async def test_clear_helper_clears_stage_and_witness(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 940103
        async with maker() as s:
            s.add(User(id=uid, recap_credits=4))
            s.add(
                _job(
                    "rc_cc_clear",
                    uid,
                    _recap_state(stranded_reset_count=2, stranded_stage="sync"),
                    status=JobStatus.FAILED.value,
                )
            )
            await s.commit()
        _write_witness("rc_cc_clear", "sync")
        witness = settings.storage_root / "recap_output" / "rc_cc_clear" / "progress_stage.json"
        assert witness.is_file()

        await api_mod._clear_recap_stranded_reset_count("rc_cc_clear")
        _, state = await _stored(maker, "rc_cc_clear")
        assert state["stranded_reset_count"] == 0
        assert state["stranded_stage"] is None
        assert not witness.exists()
    finally:
        await engine.dispose()
