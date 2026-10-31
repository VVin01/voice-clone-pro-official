"""Media retention & availability-consistency pins (fix 2026-10-31).

Background: a recap job created "yesterday" showed a false "Media Expired /
404" card claiming a server deploy had wiped temporary disk storage. In
fact (a) outputs live on the persistent disk and survive deploys, but the
app's own sweep destroyed jobs after only 24 hours; (b) the sweep deleted
files BEFORE the DB record, so a failed record delete left a SUCCESS job
with no media forever; and (c) the serializers disagreed about availability
— /admin/queue and /recap/status handed out download URLs for jobs that
/history had already flagged expired, so the media modal showed live
Download buttons for dead files.

These tests pin the fixed behaviour:
- outputs resolve under settings.storage_root (the persistent disk root);
- ONE availability check drives every serializer's deliverables;
- the sweep keeps everything inside the retention window (default 7 days)
  and purges beyond it;
- the sweep deletes the record before touching files, so it can never
  leave a SUCCESS job whose media is gone.
"""

import datetime
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.base import Base
from app.models.enums import JobStatus, JobType
from app.models.schema import Job
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod


def _make_recap_output(storage_root: Path, job_id: str) -> Path:
    """Create the exact layout the recap pipeline leaves on disk."""
    final = (
        storage_root
        / "recap_output"
        / job_id
        / "workspace"
        / "edge_tts_smart_sync"
        / "final_edge_tts_smart_sync.mp4"
    )
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"\x00" * 5000)
    (final.parent / "voice_edge_tts.wav").write_bytes(b"\x00" * 5000)
    return final


def _recap_job(job_id: str, user_id: int, created_at, result_path=None) -> Job:
    return Job(
        id=job_id,
        user_id=user_id,
        status=JobStatus.SUCCESS.value,
        job_type=JobType.RECAP.value,
        engine="recap",
        preset_voice="komin",
        input_text="https://example.com/source",
        result_path=str(result_path) if result_path else None,
        created_at=created_at,
    )


@pytest.fixture
def storage(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    root.mkdir()
    monkeypatch.setattr(settings, "storage_root", root)
    api_mod.RECAP_JOBS_REGISTRY.pop("rc_ret_t1", None)
    api_mod.RECAP_JOBS_REGISTRY.pop("rc_ret_t2", None)
    return root


# ── 1. Output paths live on the persistent storage root ─────────────────────

def test_recap_output_resolves_under_storage_root(storage):
    final = _make_recap_output(storage, "rc_ret_t1")

    # Via the recorded result_path (the normal post-completion path)…
    resolved = api_mod.resolve_recap_deliverable_path("rc_ret_t1", "video", result_path=str(final))
    assert resolved == str(final)
    # …and via the on-disk scan fallback (registry/DB path unavailable).
    resolved_scan = api_mod.resolve_recap_deliverable_path("rc_ret_t1", "video")
    assert resolved_scan == str(final)

    assert Path(resolved).is_relative_to(settings.storage_root)
    assert api_mod.check_job_file_exists("rc_ret_t1", True, str(final)) is True
    assert api_mod.check_job_file_exists("rc_ret_t1", True, None) is True
    # A job with no file on disk is honestly reported as unavailable.
    assert api_mod.check_job_file_exists("rc_ret_missing", True, None) is False


def test_retention_default_is_seven_days():
    from app.config.settings import Settings

    assert Settings().media_retention_days == 7
    assert api_mod._media_retention_days() == 7


# ── 2. One availability check drives every serializer ───────────────────────

@pytest.mark.asyncio
async def test_serializers_agree_when_media_missing(db_session, storage):
    user, _ = await get_or_create_user(db_session, telegram_id=770001, first_name="A")
    settings.admin_ids_raw = str(user.id)
    now = datetime.datetime.now(datetime.timezone.utc)
    job = _recap_job("rc_ret_t1", user.id, now, result_path=storage / "recap_output" / "rc_ret_t1" / "gone.mp4")
    db_session.add(job)
    await db_session.commit()

    hist = await api_mod.get_user_history(user=user, db=db_session)
    h = [x for x in hist["history"] if x["id"] == "rc_ret_t1"][0]
    assert h["is_expired"] is True
    assert h["download_url"] is None
    assert h["deliverables"] == {}

    adm_hist = await api_mod.get_admin_global_history(
        timeframe="24h", status="all", job_type="all", limit=100, user=user, db=db_session
    )
    ah = [x for x in adm_hist["history"] if x["id"] == "rc_ret_t1"][0]
    assert ah["is_expired"] is True
    assert ah["download_url"] is None
    assert ah["deliverables"] == {}

    queue = await api_mod.get_admin_queue(status="all", engine="all", limit=60, admin=user, db=db_session)
    q = [x for x in queue["jobs"] if x["id"] == "rc_ret_t1"][0]
    assert q["is_expired"] is True
    assert q["download_url"] is None
    assert q["deliverables"] == {}

    status = await api_mod.get_recap_status("rc_ret_t1", user=user, db=db_session)
    assert status["is_expired"] is True
    assert status["download_urls"]["video"] is None
    assert status["download_urls"]["audio"] is None
    assert status["download_urls"]["srt"] is None
    assert status["download_urls"]["script"] is None


@pytest.mark.asyncio
async def test_serializers_agree_when_media_present(db_session, storage):
    user, _ = await get_or_create_user(db_session, telegram_id=770002, first_name="B")
    settings.admin_ids_raw = str(user.id)
    now = datetime.datetime.now(datetime.timezone.utc)
    final = _make_recap_output(storage, "rc_ret_t2")
    job = _recap_job("rc_ret_t2", user.id, now, result_path=final)
    db_session.add(job)
    await db_session.commit()

    hist = await api_mod.get_user_history(user=user, db=db_session)
    h = [x for x in hist["history"] if x["id"] == "rc_ret_t2"][0]
    assert h["is_expired"] is False
    assert h["deliverables"]["video"]
    assert h["deliverables"]["audio"]

    queue = await api_mod.get_admin_queue(status="all", engine="all", limit=60, admin=user, db=db_session)
    q = [x for x in queue["jobs"] if x["id"] == "rc_ret_t2"][0]
    assert q["is_expired"] is False
    assert q["deliverables"] == h["deliverables"]
    assert q["download_url"] == h["download_url"]

    status = await api_mod.get_recap_status("rc_ret_t2", user=user, db=db_session)
    assert status["is_expired"] is False
    assert status["download_urls"]["video"]
    assert status["download_urls"]["audio"]


# ── 3. The sweep honours the retention window ───────────────────────────────

async def _file_backed_maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/sweep.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.asyncio
async def test_sweep_keeps_within_retention_and_purges_beyond(tmp_path, storage, monkeypatch):
    import app.db.session as session_mod

    engine, maker = await _file_backed_maker(tmp_path)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    now = datetime.datetime.now(datetime.timezone.utc)

    async with maker() as s:
        user, _ = await get_or_create_user(s, telegram_id=770003, first_name="C")
        uid = user.id
        ids = {}
        for label, age in [("25h", datetime.timedelta(hours=25)),
                           ("6d", datetime.timedelta(days=6)),
                           ("8d", datetime.timedelta(days=8))]:
            jid = f"rc_sweep_{label}"
            final = _make_recap_output(storage, jid)
            s.add(_recap_job(jid, uid, now - age, result_path=final))
            ids[label] = (jid, final)
        await s.commit()

    deleted = await api_mod.cleanup_expired_jobs()
    assert deleted == 1

    async with maker() as s:
        remaining = {j.id for j in (await s.execute(select(Job))).scalars().all()}
    # 25h and 6d jobs survive with files intact; the 8d job is fully purged.
    assert ids["25h"][0] in remaining and ids["25h"][1].is_file()
    assert ids["6d"][0] in remaining and ids["6d"][1].is_file()
    assert ids["8d"][0] not in remaining
    assert not ids["8d"][1].is_file()
    assert not (storage / "recap_output" / ids["8d"][0]).exists()

    await engine.dispose()


@pytest.mark.asyncio
async def test_sweep_record_deleted_even_if_file_removal_fails(tmp_path, storage, monkeypatch):
    """Ordering pin: files are removed only AFTER the record is committed
    away, so a file-removal failure can never strand a SUCCESS record whose
    media is gone (the old files-first order could)."""
    import app.db.session as session_mod

    engine, maker = await _file_backed_maker(tmp_path)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    now = datetime.datetime.now(datetime.timezone.utc)

    async with maker() as s:
        user, _ = await get_or_create_user(s, telegram_id=770004, first_name="D")
        final = _make_recap_output(storage, "rc_sweep_fail")
        s.add(_recap_job("rc_sweep_fail", user.id, now - datetime.timedelta(days=8), result_path=final))
        await s.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated rmtree failure")

    monkeypatch.setattr(api_mod.shutil, "rmtree", _boom)
    deleted = await api_mod.cleanup_expired_jobs()
    assert deleted == 1

    async with maker() as s:
        row = await s.get(Job, "rc_sweep_fail")
    assert row is None  # no zombie SUCCESS record left behind

    await engine.dispose()
