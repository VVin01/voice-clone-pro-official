"""Batch W (2026-11-04): persistent-disk retention fix — pins.

Root cause (proven from the production logs): the 10 GB persistent
disk filled up and six recap jobs died on "[Errno 28] No space left on
device" — a download write in one, the job-dir mkdir in another. The
record-driven sweep (cleanup_expired_jobs) only ever removed files a
Job row deleted in the same run pointed at, so whole classes of bulk
accumulated outside its view:

* recap_output dirs whose row was already gone — never revisited;
* FAILED/CANCELLED jobs kept their entire dir (downloaded source,
  split segments, render intermediates) for the full 7-day window,
  and the failure path stripped nothing;
* temp/recap_preview_src (a full source video per previewed URL) had
  no cleanup at all;
* voice-worker temp dirs orphaned by a dead process, and clone-flow
  uploads (temp/inputs, temp/voxcpm_refs) abandoned mid-conversation;
* and the ENOSPC error matched no classifier needle, so users saw
  the generic technical-error line.

These tests pin the Batch W behaviour: the filesystem orphan sweep
(old removed / young kept / referenced kept / outside-root and
symlink escapes untouched), the failure-path media strip (media gone,
JSON state + a completed final render kept), and the classifier's
new storage-full message.
"""

import datetime
import os
import time
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.base import Base
from app.models.enums import JobStatus, JobType
from app.models.schema import Job
from app.services.downloader import classify_source_error
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod


# ── helpers ──────────────────────────────────────────────────────────────

def _age_tree(root: Path, age_seconds: float) -> None:
    """Back-date every mtime in a tree (dirs last, so the root's own
    mtime ends up aged too)."""
    ts = time.time() - age_seconds
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            os.utime(Path(dirpath, name), (ts, ts))
        os.utime(dirpath, (ts, ts))
    os.utime(root, (ts, ts))


def _make_failed_recap_dir(storage_root: Path, job_id: str) -> Path:
    """The layout a failed recap job leaves behind: downloaded source
    at the job root, split segments + parts, the engine workspace with
    its intermediates and plan/checkpoint JSONs, a partial download."""
    job_dir = storage_root / "recap_output" / job_id
    (job_dir / "source_segments").mkdir(parents=True)
    (job_dir / "parts").mkdir(parents=True)
    ws_engine = job_dir / "workspace" / "edge_tts_smart_sync"
    for sub in ("tts_parts", "video_parts", "work"):
        (ws_engine / sub).mkdir(parents=True)
    (job_dir / "source_movie.mp4").write_bytes(b"\x00" * 9000)
    (job_dir / "source_segments" / "seg_001.mp4").write_bytes(b"\x00" * 4000)
    (job_dir / "parts" / "part_1.mp4").write_bytes(b"\x00" * 4000)
    (job_dir / "download.mp4.part").write_bytes(b"\x00" * 1000)
    (ws_engine / "tts_parts" / "line_001.wav").write_bytes(b"\x00" * 3000)
    (ws_engine / "video_parts" / "seg_001.mp4").write_bytes(b"\x00" * 3000)
    (ws_engine / "work" / "video_synced_noaudio.mp4").write_bytes(b"\x00" * 3000)
    (ws_engine / "voice_edge_tts.wav").write_bytes(b"\x00" * 3000)
    (ws_engine / "final_edge_tts_smart_sync.mp4").write_bytes(b"\x00" * 5000)
    # Small state that must survive: plans + checkpoints (JSON).
    (job_dir / "workspace" / "smart_sync_plan.json").write_text("{}", encoding="utf-8")
    (job_dir / "workspace" / "timestamp_translations.json").write_text("[]", encoding="utf-8")
    (ws_engine / "smart_sync_plan_edge_tts.json").write_text(
        '{"fallback_used": true, "fallback_segments": [1]}', encoding="utf-8"
    )
    (ws_engine / "checkpoint.json").write_text("{}", encoding="utf-8")
    return job_dir


def _job(job_id: str, user_id: int, status: str, created_at, **kw) -> Job:
    return Job(
        id=job_id,
        user_id=user_id,
        status=status,
        job_type=kw.pop("job_type", JobType.RECAP.value),
        engine=kw.pop("engine", "recap"),
        preset_voice="komin",
        input_text="https://example.com/source",
        created_at=created_at,
        **kw,
    )


@pytest.fixture
def storage(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    root.mkdir()
    monkeypatch.setattr(settings, "storage_root", root)
    return root


async def _file_backed_maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/sweep_w.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def sweep_db(tmp_path, monkeypatch):
    import app.db.session as session_mod

    async def _setup():
        engine, maker = await _file_backed_maker(tmp_path)
        monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
        return engine, maker

    return _setup


# ── 1. Classifier: ENOSPC gets an honest storage message ─────────────────

def test_classifier_maps_enospc_download_error():
    # The exact production string of job rc_1793783899_9d2cb2.
    msg = classify_source_error(
        "Unable to download video: [Errno 28] No space left on device",
        "https://www.youtube.com/watch?v=GlunZpix30I",
    )
    assert msg is not None
    assert msg.startswith("⚠️")
    assert "သိုလှောင်" in msg  # storage named honestly, no raw English leak
    assert "Errno" not in msg and "space left" not in msg


def test_classifier_maps_enospc_mkdir_error():
    # The exact production string of job rc_1793788646_741dd4.
    msg = classify_source_error(
        "OSError: [Errno 28] No space left on device: '/app/storage/recap_output/rc_x'",
        "https://www.youtube.com/watch?v=GlunZpix30I",
    )
    assert msg is not None and "သိုလှောင်" in msg


def test_classifier_enospc_needle_does_not_overreach():
    # Neighbouring classes keep their own messages; unknown errors
    # still classify to None (default-deny fallback stays in charge).
    assert "ဖွင့်၍ မရပါ" in classify_source_error("Video unavailable", "https://youtu.be/x")
    assert classify_source_error("some totally unknown failure", "https://youtu.be/x") is None


# ── 2. Failure-path strip: media gone, state kept ────────────────────────

def test_strip_failed_job_media_keeps_json_state_and_final(storage):
    job_dir = _make_failed_recap_dir(storage, "rc_w_failed")
    removed, freed = api_mod._strip_failed_recap_job_media(job_dir)
    assert removed >= 7 and freed > 0
    # Media bulk is gone…
    assert not (job_dir / "source_movie.mp4").exists()
    assert not (job_dir / "source_segments" / "seg_001.mp4").exists()
    assert not (job_dir / "parts" / "part_1.mp4").exists()
    assert not (job_dir / "download.mp4.part").exists()
    engine_dir = job_dir / "workspace" / "edge_tts_smart_sync"
    assert not (engine_dir / "tts_parts" / "line_001.wav").exists()
    assert not (engine_dir / "video_parts" / "seg_001.mp4").exists()
    assert not (engine_dir / "work" / "video_synced_noaudio.mp4").exists()
    assert not (engine_dir / "voice_edge_tts.wav").exists()
    # …but the small state survives: plans, checkpoints, fallback
    # markers — and a completed final render is never destroyed.
    assert (job_dir / "workspace" / "smart_sync_plan.json").is_file()
    assert (job_dir / "workspace" / "timestamp_translations.json").is_file()
    assert (engine_dir / "smart_sync_plan_edge_tts.json").is_file()
    assert (engine_dir / "checkpoint.json").is_file()
    assert (engine_dir / "final_edge_tts_smart_sync.mp4").is_file()
    # The fallback-transparency reader still finds the marker.
    used, segs = api_mod._recap_fallback_info(job_dir)
    assert used is True and segs == [1]


def test_strip_never_follows_symlink_out_of_the_job_dir(storage, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "keep.mp4"
    secret.write_bytes(b"\x00" * 100)
    job_dir = storage / "recap_output" / "rc_w_link"
    job_dir.mkdir(parents=True)
    (job_dir / "real.mp4").write_bytes(b"\x00" * 100)
    os.symlink(outside, job_dir / "linked_dir")
    os.symlink(secret, job_dir / "linked.mp4")
    api_mod._strip_failed_recap_job_media(job_dir)
    assert secret.is_file()  # outside content untouched
    assert not (job_dir / "real.mp4").exists()


def test_strip_on_symlinked_job_dir_is_a_noop(storage, tmp_path):
    real = tmp_path / "real_job"
    real.mkdir()
    (real / "source.mp4").write_bytes(b"\x00" * 100)
    link = storage / "recap_output" / "rc_w_dirlink"
    link.parent.mkdir(parents=True)
    os.symlink(real, link)
    assert api_mod._strip_failed_recap_job_media(link) == (0, 0)
    assert (real / "source.mp4").is_file()


# ── 3. The orphan sweep inside cleanup_expired_jobs ──────────────────────

@pytest.mark.asyncio
async def test_sweep_removes_old_orphan_recap_dir_keeps_young(storage, sweep_db):
    engine, maker = await sweep_db()
    old_dir = _make_failed_recap_dir(storage, "rc_w_orphan_old")
    _age_tree(old_dir, 8 * 86400)
    young_dir = _make_failed_recap_dir(storage, "rc_w_orphan_young")
    # Batch X: the retention window is 24h now — "young" must be
    # comfortably inside it (12h), not exactly on the old boundary.
    _age_tree(young_dir, 12 * 3600)

    await api_mod.cleanup_expired_jobs()

    assert not old_dir.exists()  # no row + older than the window: gone whole
    assert young_dir.is_dir() and (young_dir / "source_movie.mp4").is_file()
    await engine.dispose()


@pytest.mark.asyncio
async def test_sweep_strips_failed_media_but_spares_success_and_running(storage, sweep_db):
    engine, maker = await sweep_db()
    now = datetime.datetime.now(datetime.timezone.utc)
    async with maker() as s:
        user, _ = await get_or_create_user(s, telegram_id=770101, first_name="W")
        uid = user.id
        # Batch X: rows are created 12h ago so they sit INSIDE the
        # 24-hour retention window (the record sweep must not take
        # them); the dirs below stay aged 2 days so the FAILED strip
        # threshold (dir mtime > 24h) is what fires.
        s.add(_job("rc_w_sfailed", uid, JobStatus.FAILED.value, now - datetime.timedelta(hours=12)))
        s.add(_job("rc_w_ssuccess", uid, JobStatus.SUCCESS.value, now - datetime.timedelta(hours=12)))
        s.add(_job("rc_w_srunning", uid, JobStatus.RUNNING.value, now - datetime.timedelta(hours=12)))
        await s.commit()

    failed_dir = _make_failed_recap_dir(storage, "rc_w_sfailed")
    _age_tree(failed_dir, 2 * 86400)
    success_dir = _make_failed_recap_dir(storage, "rc_w_ssuccess")
    _age_tree(success_dir, 2 * 86400)
    running_dir = _make_failed_recap_dir(storage, "rc_w_srunning")
    _age_tree(running_dir, 2 * 86400)

    await api_mod.cleanup_expired_jobs()

    # FAILED: media stripped after 24h, row + JSON state remain.
    assert not (failed_dir / "source_movie.mp4").exists()
    assert (failed_dir / "workspace" / "smart_sync_plan.json").is_file()
    # SUCCESS and RUNNING dirs are never touched by the strip.
    assert (success_dir / "source_movie.mp4").is_file()
    assert (running_dir / "source_movie.mp4").is_file()
    async with maker() as s:
        remaining = {j.id for j in (await s.execute(select(Job))).scalars().all()}
    assert remaining == {"rc_w_sfailed", "rc_w_ssuccess", "rc_w_srunning"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_sweep_preview_cache_and_temp_artefacts(storage, sweep_db):
    engine, maker = await sweep_db()
    now = datetime.datetime.now(datetime.timezone.utc)
    async with maker() as s:
        user, _ = await get_or_create_user(s, telegram_id=770102, first_name="X")
        uid = user.id
        s.add(_job("9101", uid, JobStatus.FAILED.value, now - datetime.timedelta(days=2),
                   job_type=JobType.VOICE_CLONE.value, engine="voxcpm"))
        s.add(_job("9102", uid, JobStatus.RUNNING.value, now - datetime.timedelta(days=2),
                   job_type=JobType.VOICE_CLONE.value, engine="voxcpm"))
        kept_input = storage / "temp" / "inputs" / "input_kept_ref.wav"
        kept_input.parent.mkdir(parents=True, exist_ok=True)
        kept_input.write_bytes(b"\x00" * 100)
        _age_tree(kept_input.parent, 2 * 86400)  # aged, but referenced below
        # Batch X: 9103 sits inside the 24h retention window (12h) so
        # its row — and the reference protecting kept_input — survives
        # the record sweep; exactly-1-day was the old boundary case.
        s.add(_job("9103", uid, JobStatus.SUCCESS.value, now - datetime.timedelta(hours=12),
                   job_type=JobType.VOICE_CLONE.value, engine="voxcpm",
                   source_audio_path=str(kept_input)))
        await s.commit()

    # Preview-source cache: 2d-old URL dir removed, 1h-old kept.
    preview_old = storage / "temp" / "recap_preview_src" / "urlkey_old"
    preview_old.mkdir(parents=True)
    (preview_old / "source.mp4").write_bytes(b"\x00" * 5000)
    _age_tree(preview_old, 2 * 86400)
    preview_new = storage / "temp" / "recap_preview_src" / "urlkey_new"
    preview_new.mkdir(parents=True, exist_ok=True)
    (preview_new / "source.mp4").write_bytes(b"\x00" * 5000)

    # Voice-worker temp dirs: terminal job's leftover + unknown id go;
    # the RUNNING job's stays.
    temp_dead = storage / "temp" / "9101"
    temp_dead.mkdir()
    (temp_dead / "work.wav").write_bytes(b"\x00" * 100)
    _age_tree(temp_dead, 2 * 86400)
    temp_live = storage / "temp" / "9102"
    temp_live.mkdir(exist_ok=True)
    (temp_live / "work.wav").write_bytes(b"\x00" * 100)
    _age_tree(temp_live, 2 * 86400)
    temp_unknown = storage / "temp" / "9999"
    temp_unknown.mkdir(exist_ok=True)
    (temp_unknown / "work.wav").write_bytes(b"\x00" * 100)
    _age_tree(temp_unknown, 2 * 86400)

    # Abandoned clone uploads: unreferenced + old removed; referenced
    # kept at any age; young unreferenced kept.
    orphan_input = storage / "temp" / "inputs" / "input_orphan.wav"
    orphan_input.write_bytes(b"\x00" * 100)
    _age_tree(storage / "temp" / "inputs", 2 * 86400)
    young_ref = storage / "temp" / "voxcpm_refs" / "ref_young.wav"
    young_ref.parent.mkdir(parents=True, exist_ok=True)
    young_ref.write_bytes(b"\x00" * 100)

    await api_mod.cleanup_expired_jobs()

    assert not preview_old.exists()
    assert preview_new.is_dir()
    assert not temp_dead.exists()
    assert not temp_unknown.exists()
    assert temp_live.is_dir()
    assert not orphan_input.exists()
    assert kept_input.is_file()
    assert young_ref.is_file()
    await engine.dispose()


@pytest.mark.asyncio
async def test_sweep_never_removes_through_a_symlink(storage, sweep_db, tmp_path):
    engine, maker = await sweep_db()
    outside = tmp_path / "outside_storage"
    outside.mkdir()
    precious = outside / "precious.mp4"
    precious.write_bytes(b"\x00" * 100)
    _age_tree(outside, 9 * 86400)
    recap_root = storage / "recap_output"
    recap_root.mkdir(parents=True)
    link = recap_root / "rc_w_escape"
    os.symlink(outside, link)

    await api_mod.cleanup_expired_jobs()

    assert precious.is_file()  # the outside tree is untouched
    assert link.is_symlink()  # and the symlink itself is left alone
    await engine.dispose()


# ── 4. Wiring pins: failure paths + periodic sweep ───────────────────────

def test_failure_handlers_and_sweep_are_wired():
    src = Path(api_mod.__file__).read_text(encoding="utf-8")
    # Both recap failure handlers (generic + cancellation) strip media.
    assert src.count("_strip_failed_recap_job_media(job_dir)") >= 2
    # The periodic/startup sweep runs the filesystem orphan pass.
    assert "await _sweep_storage_orphans(retention_cutoff_ts=cutoff.timestamp())" in src
