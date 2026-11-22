"""Regression tests for 2026-11-22 production fixes:

Bug 1 — Worker-claimed Recap jobs must upload their full deliverable bundle
        (MP4, voiceover MP3/WAV, SRT, ASS, script TXT, split parts, plan JSON)
        to the web service before marking SUCCESS or sending the Telegram
        completion notification, while web-claimed jobs perform zero uploads.

Bug 2 — Subtitle repack (Round 10 Part D) must merge short planner fragments
        into full-width cues across burned PNG cues, downloadable SRT, and ASS
        sidecar in the production recap_pipeline -> EdgeSmartSync.run path,
        normalizing both segment-relative and job-absolute word_timings.
"""

from __future__ import annotations

import io
import json
import logging
import math
import subprocess
import wave
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy import event

from app.config import settings
from app.db.base import Base
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Job, RecapCreditLedgerEvent, User
from app.services.ledger import get_or_create_user
from app.services.recap_engine import core, png_subtitles as _png_subs, recap_pipeline
from app.webapp import api as api_mod
from app.webapp import rate_limit as rl_mod
from app.worker import (
    collect_recap_bundle_files,
    process_recap_job,
    sync_recap_bundle_to_cloud,
)

TEST_WORKER_TOKEN = "worker-secret-token-2026-11-22"
TEST_BOT_TOKEN = "123456:test-bot-token-2026-11-22"


async def _make_isolated_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        echo=False,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _pragma(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    return engine, maker


def _upload_file(filename: str, content: bytes) -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(content))


async def _seed_recap_job(
    session: AsyncSession,
    job_id: str = "rc_1795330795_791e90",
    telegram_id: int = 770011,
    status: str = JobStatus.RUNNING.value,
) -> tuple[User, Job]:
    user, _ = await get_or_create_user(session, telegram_id=telegram_id)
    user.account_category = AccountCategory.PURCHASED.value
    user.recap_credits = 5
    req = api_mod.RecapConfigRequest(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        title="Buldak Ramen Recap",
        voice_slug="komin",
        tts_engine="edge",
        duration_seconds=59,
    )
    job = Job(
        id=job_id,
        user_id=user.id,
        chat_id=user.id,
        status=status,
        job_type=JobType.RECAP.value,
        engine="recap",
        preset_voice="komin",
        input_text="Buldak Ramen Recap",
        source_audio_path=req.url,
        seconds_estimated=59,
        recap_config=api_mod._persisted_recap_state(
            req,
            credit_deducted=True,
            credits_charged=1.0,
            is_vip=False,
            phase="full",
            estimated_parts=1,
            source_duration_seconds=59.0,
            tiered_pricing=True,
            max_quality_surcharge=0,
            billing_mode="credit_only",
            recap_seconds_held=60,
            voice_seconds_held=0,
        ),
    )
    session.add(job)
    await session.commit()
    return user, job


# ── BUG 1: Internal Endpoint Security & Validation Tests ─────────────────────


def test_recap_upload_path_registered_in_max_content_length():
    assert rl_mod.MAX_CONTENT_LENGTH.get("/api/internal/worker/upload-recap-file") == 500 * 1024 * 1024
    # Voice upload cap remains untouched
    assert rl_mod.MAX_CONTENT_LENGTH.get("/api/internal/worker/upload-result") == 500 * 1024 * 1024


@pytest.mark.asyncio
async def test_recap_upload_endpoint_404_when_worker_token_unset(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "worker_token", "")
    monkeypatch.setattr(settings, "bot_token", TEST_BOT_TOKEN)
    await _seed_recap_job(db_session)

    for candidate_token in (TEST_WORKER_TOKEN, TEST_BOT_TOKEN, ""):
        with pytest.raises(HTTPException) as exc:
            await api_mod.upload_worker_recap_file(
                "rc_1795330795_791e90",
                file=_upload_file("final_edge_tts_smart_sync.mp4", b"fake-mp4"),
                rel_path="workspace/edge_tts_smart_sync/final_edge_tts_smart_sync.mp4",
                role="video",
                auth_token=candidate_token,
                db=db_session,
            )
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_recap_upload_endpoint_403_on_wrong_or_missing_token(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "worker_token", TEST_WORKER_TOKEN)
    monkeypatch.setattr(settings, "bot_token", TEST_BOT_TOKEN)
    await _seed_recap_job(db_session)

    for bad_token in ("wrong-token", TEST_BOT_TOKEN, "", None):
        with pytest.raises(HTTPException) as exc:
            await api_mod.upload_worker_recap_file(
                "rc_1795330795_791e90",
                file=_upload_file("final_edge_tts_smart_sync.mp4", b"fake-mp4"),
                rel_path="workspace/edge_tts_smart_sync/final_edge_tts_smart_sync.mp4",
                role="video",
                auth_token=bad_token,
                db=db_session,
            )
        assert exc.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [JobStatus.CANCELLED.value, JobStatus.FAILED.value, JobStatus.SUCCESS.value],
)
async def test_recap_upload_endpoint_409_on_terminal_job(
    db_session, tmp_path, monkeypatch, terminal_status
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "worker_token", TEST_WORKER_TOKEN)
    monkeypatch.setattr(api_mod, "DISK_HEADROOM_MIN_FREE_BYTES", 0)
    jid = f"rc_term_{terminal_status.lower()}"
    await _seed_recap_job(db_session, job_id=jid, status=terminal_status)

    with pytest.raises(HTTPException) as exc:
        await api_mod.upload_worker_recap_file(
            jid,
            file=_upload_file("final_edge_tts_smart_sync.mp4", b"fake-mp4"),
            rel_path="workspace/edge_tts_smart_sync/final_edge_tts_smart_sync.mp4",
            role="video",
            auth_token=TEST_WORKER_TOKEN,
            db=db_session,
        )
    assert exc.value.status_code == 409
    assert not (tmp_path / "recap_output" / jid).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_rel",
    [
        "../outside.mp4",
        "workspace/../../outside.mp4",
        "/abs/path/video.mp4",
        "workspace\\backslash\\video.mp4",
        "C:/drive/video.mp4",
        "workspace/payload.exe",
        "workspace/script.py",
        "",
    ],
)
async def test_recap_upload_endpoint_rejects_traversal_and_bad_extensions(
    db_session, tmp_path, monkeypatch, bad_rel
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "worker_token", TEST_WORKER_TOKEN)
    monkeypatch.setattr(api_mod, "DISK_HEADROOM_MIN_FREE_BYTES", 0)
    await _seed_recap_job(db_session)

    with pytest.raises(HTTPException) as exc:
        await api_mod.upload_worker_recap_file(
            "rc_1795330795_791e90",
            file=_upload_file("file.bin", b"data"),
            rel_path=bad_rel,
            role="video",
            auth_token=TEST_WORKER_TOKEN,
            db=db_session,
        )
    assert exc.value.status_code == 400


class _ZeroStream:
    def __init__(self, total_bytes: int):
        self._rem = total_bytes

    def read(self, n: int = -1) -> bytes:
        if self._rem <= 0:
            return b""
        take = min(n if n and n > 0 else 1024 * 1024, self._rem)
        self._rem -= take
        return b"0" * take


@pytest.mark.asyncio
async def test_recap_upload_endpoint_413_on_oversized_file_and_cleans_temp(
    db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "worker_token", TEST_WORKER_TOKEN)
    monkeypatch.setattr(api_mod, "DISK_HEADROOM_MIN_FREE_BYTES", 0)
    jid = "rc_1795330795_791e90"
    await _seed_recap_job(db_session, job_id=jid)

    oversized_srt = UploadFile(
        filename="recap_myanmar_synced.srt",
        file=_ZeroStream(11 * 1024 * 1024),  # > 10 MB limit for .srt
    )
    with pytest.raises(HTTPException) as exc:
        await api_mod.upload_worker_recap_file(
            jid,
            file=oversized_srt,
            rel_path="workspace/edge_tts_smart_sync/recap_myanmar_synced.srt",
            role="srt",
            auth_token=TEST_WORKER_TOKEN,
            db=db_session,
        )
    assert exc.value.status_code == 413
    target_dir = tmp_path / "recap_output" / jid / "workspace" / "edge_tts_smart_sync"
    if target_dir.exists():
        assert list(target_dir.iterdir()) == []


# ── BUG 1: Worker-Mode vs Web-Mode Recap Completion Flow ─────────────────────


def _populate_fake_worker_recap_outputs(job_dir: Path, job_id: str) -> dict[str, Any]:
    """Write canonical recap deliverables on the worker's local storage root."""
    sync_dir = job_dir / "workspace" / "edge_tts_smart_sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    # Also write intermediate scratch files that MUST NOT be uploaded
    work_dir = sync_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "scratch_part.mp4").write_bytes(b"scratch-ignore")
    (job_dir / "source_video.mp4").write_bytes(b"raw-source-ignore")

    mp4 = sync_dir / "final_edge_tts_smart_sync.mp4"
    mp3 = sync_dir / "final_edge_tts_smart_sync.mp3"
    srt = sync_dir / "final_edge_tts_smart_sync.srt"
    srt_synced = sync_dir / "recap_myanmar_synced.srt"
    ass = sync_dir / "final_edge_tts_smart_sync.ass"
    ass_synced = sync_dir / "recap_myanmar_synced.ass"
    plan_json = sync_dir / "smart_sync_plan_edge_tts.json"

    mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"V" * 4096)
    mp3.write_bytes(b"ID3" + b"A" * 2048)
    srt_body = (
        "1\n00:00:00,000 --> 00:00:02,157\n"
        "ကျွန်တော် ဘူဒတ် ပြင်လိုက်တယ်။ ပထမဆုံးတစ်လုပ်ကို\n\n"
        "2\n00:00:02,157 --> 00:00:05,400\n"
        "စားလိုက်တာနဲ့ အရမ်းစပ်သွားတယ်။\n"
    )
    srt.write_text(srt_body, encoding="utf-8")
    srt_synced.write_text(srt_body, encoding="utf-8")
    ass_body = "[Script Info]\nTitle: Synced ASS\n[Events]\nDialogue: 0,0:00:00.00,0:00:02.16,Default,,0,0,0,,ကျွန်တော် ဘူဒတ် ပြင်လိုက်တယ်။\n"
    ass.write_text(ass_body, encoding="utf-8")
    ass_synced.write_text(ass_body, encoding="utf-8")
    plan_json.write_text(
        json.dumps(
            {
                "fallback_used": False,
                "fallback_segments": [],
                "reference_trimmed": False,
                "tts_timing_segments": [
                    {"audio_start": 0.0, "audio_end": 2.157, "text": "ကျွန်တော် ဘူဒတ် ပြင်လိုက်တယ်။ ပထမဆုံးတစ်လုပ်ကို"},
                    {"audio_start": 2.157, "audio_end": 5.4, "text": "စားလိုက်တာနဲ့ အရမ်းစပ်သွားတယ်။"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {
        "final_video": str(mp4),
        "audio": str(mp3),
        "subtitles_srt": str(srt),
        "subtitles_ass": str(ass),
        "plan_path": str(plan_json),
        "job_dir": str(job_dir),
        "segments": 2,
        "duration": 5.4,
        "actual_voice_seconds": 0,
        "tts_runtime_seconds": 1.1,
        "api_usage": {},
        "warnings": [],
        "stage_timings": {"tts": 1.1, "sync": 1.5},
    }


@pytest.mark.asyncio
async def test_worker_mode_recap_completion_uploads_full_bundle_before_success_and_notify(
    tmp_path, monkeypatch
):
    """Simulates vcp-worker (worker_storage) + vcp-web (web_storage) split.

    Verifies:
    1. Every deliverable (MP4, MP3, SRT, ASS, script TXT, plan JSON) lands on
       web_storage at the canonical path under recap_output/<job_id>/...
    2. Uploads complete strictly BEFORE Job.status becomes SUCCESS and BEFORE
       the Telegram completion notification is sent.
    3. Bot/Mini App path lookups and download_recap_file on vcp-web find the
       uploaded files even when vcp-web's RAM registry had a stale QUEUED entry.
    """
    worker_storage = tmp_path / "worker_storage"
    web_storage = tmp_path / "web_storage"
    worker_storage.mkdir()
    web_storage.mkdir()

    engine, session_maker = await _make_isolated_db()
    try:
        monkeypatch.setattr(api_mod, "AsyncSessionLocal", session_maker)
        monkeypatch.setattr("app.db.session.AsyncSessionLocal", session_maker)
        monkeypatch.setattr(settings, "storage_root", worker_storage)
        monkeypatch.setattr(settings, "instance_primary", False)
        monkeypatch.setattr(settings, "worker_token", TEST_WORKER_TOKEN)
        monkeypatch.setattr(settings, "bot_token", TEST_BOT_TOKEN)
        monkeypatch.setattr(api_mod, "DISK_HEADROOM_MIN_FREE_BYTES", 0)
        api_mod.RECAP_JOBS_REGISTRY.clear()

        jid = "rc_1795330795_791e90"
        async with session_maker() as s:
            user, _ = await _seed_recap_job(s, job_id=jid, status=JobStatus.RUNNING.value)
            uid = user.id

        events: list[str] = []

        def _fake_generate_recap_video(self, url_or_path=None, target_dir=None, **kwargs):
            events.append("render_done")
            return _populate_fake_worker_recap_outputs(Path(target_dir), jid)

        monkeypatch.setattr(
            "app.services.downloader.MediaDownloader.generate_recap_video",
            _fake_generate_recap_video,
        )
        monkeypatch.setattr(
            "app.services.downloader.probe_local_media",
            lambda _p: {"duration": 5.4},
        )

        class _FakeResp:
            def __init__(self, status_code: int, body: dict[str, Any]):
                self.status_code = status_code
                self.is_success = 200 <= status_code < 300
                self._body = body
                self.text = json.dumps(body)

            def json(self):
                return self._body

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, *, files=None, data=None, headers=None):
                assert url.endswith(f"/api/internal/worker/upload-recap-file/{jid}")
                rel_p = (data or {}).get("rel_path")
                role = (data or {}).get("role")
                fname, fh, _mime = files["file"]
                raw_bytes = fh.read()

                # Verify DB status is still RUNNING (not SUCCESS) during upload!
                async with session_maker() as check_s:
                    cur_job = await check_s.get(Job, jid)
                    assert cur_job.status == JobStatus.RUNNING.value
                    events.append(f"upload:{role}:{rel_p}:status={cur_job.status}")

                # Execute the real FastAPI upload endpoint against web_storage!
                prev_root = settings.storage_root
                settings.storage_root = web_storage
                try:
                    async with session_maker() as web_s:
                        res = await api_mod.upload_worker_recap_file(
                            jid,
                            file=_upload_file(fname, raw_bytes),
                            rel_path=rel_p,
                            role=role,
                            auth_token=(headers or {}).get("X-Worker-Token"),
                            db=web_s,
                        )
                    return _FakeResp(200, res)
                finally:
                    settings.storage_root = prev_root

        monkeypatch.setattr("httpx.AsyncClient", _FakeAsyncClient)

        class _FakeBotSession:
            async def close(self):
                pass

        class _FakeBot:
            def __init__(self):
                self.session = _FakeBotSession()

            async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
                async with session_maker() as check_s:
                    cur_job = await check_s.get(Job, jid)
                    events.append(f"telegram_notify:status={cur_job.status}")
                assert (web_storage / "recap_output" / jid / "workspace" / "edge_tts_smart_sync" / "final_edge_tts_smart_sync.mp4").is_file()

        monkeypatch.setattr(api_mod, "create_telegram_bot", lambda **kw: _FakeBot())

        async with session_maker() as worker_s:
            job_row = await worker_s.get(Job, jid)
            ok = await process_recap_job(worker_s, job_row)
            assert ok is True

        # 1. Verify strict ordering: render -> uploads (while RUNNING) -> telegram_notify (after SUCCESS)
        assert events[0] == "render_done"
        upload_events = [e for e in events if e.startswith("upload:")]
        assert len(upload_events) >= 6
        assert all(e.endswith(":status=RUNNING") for e in upload_events)
        assert events[-1] == "telegram_notify:status=SUCCESS"

        # 2. Verify all canonical files exist on web_storage and scratch files do NOT
        web_job_dir = web_storage / "recap_output" / jid
        web_sync_dir = web_job_dir / "workspace" / "edge_tts_smart_sync"
        assert (web_sync_dir / "final_edge_tts_smart_sync.mp4").is_file()
        assert (web_sync_dir / "final_edge_tts_smart_sync.mp3").is_file()
        assert (web_sync_dir / "final_edge_tts_smart_sync.srt").is_file()
        assert (web_sync_dir / "recap_myanmar_synced.srt").is_file()
        assert (web_sync_dir / "final_edge_tts_smart_sync.ass").is_file()
        assert (web_sync_dir / "recap_myanmar_synced.ass").is_file()
        assert (web_sync_dir / "smart_sync_plan_edge_tts.json").is_file()
        assert (web_job_dir / f"{jid}_script.txt").is_file()
        assert not (web_job_dir / "source_video.mp4").exists()
        assert not (web_sync_dir / "work").exists()

        # 3. Switch to vcp-web view (settings.storage_root = web_storage) with a
        # stale QUEUED registry entry (exactly as when vcp-web enqueued the job).
        settings.storage_root = web_storage
        api_mod.RECAP_JOBS_REGISTRY[jid] = {
            "user_id": uid,
            "status": "QUEUED",
            "title": "Buldak Ramen Recap",
        }
        async with session_maker() as web_s:
            finished_job = await web_s.get(Job, jid)
            assert finished_job.status == JobStatus.SUCCESS.value
            assert finished_job.result_path == str(web_sync_dir / "final_edge_tts_smart_sync.mp4")

            # Bot send-handler lookup (resolve_recap_deliverable_path) finds every deliverable on web_storage
            for ftype in ("video", "audio", "srt", "script"):
                resolved = api_mod.resolve_recap_deliverable_path(
                    jid, ftype, result_path=finished_job.result_path
                )
                assert resolved is not None and Path(resolved).is_file(), f"Missing {ftype} on web_storage"
                assert Path(resolved).is_relative_to(web_storage)

            # Mini App download_recap_file reconciles stale QUEUED registry entry and serves 200
            web_user = await web_s.get(User, uid)
            scope = {
                "type": "http",
                "method": "GET",
                "path": f"/api/recap/download/{jid}/video",
                "headers": [],
                "client": ("127.0.0.1", 9000),
            }
            dl_resp = await api_mod.download_recap_file(
                jid,
                "video",
                request=api_mod.Request(scope),
                user=web_user,
                t=None,
                db=web_s,
            )
            assert Path(dl_resp.path) == web_sync_dir / "final_edge_tts_smart_sync.mp4"
            assert api_mod.RECAP_JOBS_REGISTRY[jid]["status"] == "SUCCESS"
    finally:
        api_mod.RECAP_JOBS_REGISTRY.clear()
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_mode_upload_failure_retries_logs_loudly_and_fails_without_notify(
    tmp_path, monkeypatch, caplog
):
    """When bundle upload fails after retries, job must fail/refund and NEVER send completion notify."""
    worker_storage = tmp_path / "worker_storage"
    worker_storage.mkdir()

    engine, session_maker = await _make_isolated_db()
    try:
        monkeypatch.setattr(api_mod, "AsyncSessionLocal", session_maker)
        monkeypatch.setattr("app.db.session.AsyncSessionLocal", session_maker)
        monkeypatch.setattr(settings, "storage_root", worker_storage)
        monkeypatch.setattr(settings, "instance_primary", False)
        monkeypatch.setattr(settings, "worker_token", TEST_WORKER_TOKEN)
        monkeypatch.setattr(settings, "bot_token", TEST_BOT_TOKEN)
        api_mod.RECAP_JOBS_REGISTRY.clear()

        jid = "rc_upload_fail_01"
        async with session_maker() as s:
            await _seed_recap_job(s, job_id=jid, status=JobStatus.RUNNING.value)

        monkeypatch.setattr(
            "app.services.downloader.MediaDownloader.generate_recap_video",
            lambda self, url_or_path=None, target_dir=None, **kw: _populate_fake_worker_recap_outputs(Path(target_dir), jid),
        )

        post_attempts = []

        class _FailingResp:
            status_code = 502
            is_success = False
            text = "Bad Gateway"

        class _FailingAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, **kwargs):
                post_attempts.append(url)
                return _FailingResp()

        monkeypatch.setattr("httpx.AsyncClient", _FailingAsyncClient)
        # Speed up retry backoff in test
        orig_sync = sync_recap_bundle_to_cloud

        async def _fast_sync(job_id, job_dir, bundle_files, **kw):
            return await orig_sync(job_id, job_dir, bundle_files, max_attempts=3, retry_delay=0.0)

        monkeypatch.setattr("app.worker.sync_recap_bundle_to_cloud", _fast_sync)

        sent_messages: list[str] = []

        class _FakeBotSession:
            async def close(self):
                pass

        class _FakeBot:
            def __init__(self):
                self.session = _FakeBotSession()

            async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
                sent_messages.append(text)

        monkeypatch.setattr(api_mod, "create_telegram_bot", lambda **kw: _FakeBot())

        with caplog.at_level(logging.ERROR):
            async with session_maker() as worker_s:
                job_row = await worker_s.get(Job, jid)
                ok = await process_recap_job(worker_s, job_row)
                assert ok is False

        # Bounded retry: 3 attempts on the first file before aborting
        assert len(post_attempts) == 3
        assert any("FAILED after 3 attempt(s)" in r.getMessage() or "bundle sync to web failed" in r.getMessage() for r in caplog.records)

        async with session_maker() as s:
            failed_job = await s.get(Job, jid)
            assert failed_job.status == JobStatus.FAILED.value

        # No completion notification with send_recap buttons was sent
        assert not any("send_recap_" in m or "Recap Video Generation Completed" in m for m in sent_messages)
    finally:
        api_mod.RECAP_JOBS_REGISTRY.clear()
        await engine.dispose()


@pytest.mark.asyncio
async def test_web_mode_recap_completion_performs_no_upload(tmp_path, monkeypatch):
    """Web-claimed recap jobs (instance_primary=True) perform zero HTTP uploads."""
    web_storage = tmp_path / "web_storage"
    web_storage.mkdir()

    engine, session_maker = await _make_isolated_db()
    try:
        monkeypatch.setattr(api_mod, "AsyncSessionLocal", session_maker)
        monkeypatch.setattr("app.db.session.AsyncSessionLocal", session_maker)
        monkeypatch.setattr(settings, "storage_root", web_storage)
        monkeypatch.setattr(settings, "instance_primary", True)
        monkeypatch.setattr(settings, "worker_token", TEST_WORKER_TOKEN)
        monkeypatch.setattr(settings, "bot_token", "")
        api_mod.RECAP_JOBS_REGISTRY.clear()

        jid = "rc_web_primary_01"
        async with session_maker() as s:
            await _seed_recap_job(s, job_id=jid, status=JobStatus.RUNNING.value)

        monkeypatch.setattr(
            "app.services.downloader.MediaDownloader.generate_recap_video",
            lambda self, url_or_path=None, target_dir=None, **kw: _populate_fake_worker_recap_outputs(Path(target_dir), jid),
        )
        monkeypatch.setattr(
            "app.services.downloader.probe_local_media",
            lambda _p: {"duration": 5.4},
        )

        http_calls = []

        class _ForbiddenAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, *args, **kwargs):
                http_calls.append((args, kwargs))
                raise AssertionError("Web-claimed recap job must not perform worker HTTP upload")

        monkeypatch.setattr("httpx.AsyncClient", _ForbiddenAsyncClient)

        async with session_maker() as s:
            job_row = await s.get(Job, jid)
            ok = await process_recap_job(s, job_row)
            assert ok is True

        assert http_calls == []
        async with session_maker() as s:
            done_job = await s.get(Job, jid)
            assert done_job.status == JobStatus.SUCCESS.value
    finally:
        api_mod.RECAP_JOBS_REGISTRY.clear()
        await engine.dispose()


# ── BUG 2: Production-Shaped Burmese Subtitle Repack & word_timings ──────────


def _production_shaped_buldak_segments() -> list[dict[str, Any]]:
    """Production-shaped planner segment stream (short fragments + >0.35s pause)
    with segment-relative EdgeTTSEngine word_timings (0.0..raw_dur per segment).
    """
    raw_specs = [
        # Production job rc_1795330795_791e90 opening fragments:
        (0.000, 0.546, "ကျွန်တော် ဘူဒတ်", [
            {"start": 0.05, "end": 0.28, "text": "ကျွန်တော်"},
            {"start": 0.28, "end": 0.52, "text": "ဘူဒတ်"},
        ]),
        (0.546, 1.184, "ပြင်လိုက်တယ်။", [
            {"start": 0.04, "end": 0.32, "text": "ပြင်"},
            {"start": 0.32, "end": 0.61, "text": "လိုက်တယ်။"},
        ]),
        (1.184, 2.157, "ပထမဆုံးတစ်လုပ်ကို", [
            {"start": 0.05, "end": 0.50, "text": "ပထမဆုံး"},
            {"start": 0.50, "end": 0.94, "text": "တစ်လုပ်ကို"},
        ]),
        (2.157, 4.100, "စားလိုက်တာနဲ့ အရမ်းစပ်သွားတယ်။", [
            {"start": 0.06, "end": 0.88, "text": "စားလိုက်တာနဲ့"},
            {"start": 0.88, "end": 1.90, "text": "အရမ်းစပ်သွားတယ်။"},
        ]),
        (4.100, 5.400, "ဒါပေမဲ့ အရသာက", [
            {"start": 0.05, "end": 0.60, "text": "ဒါပေမဲ့"},
            {"start": 0.60, "end": 1.25, "text": "အရသာက"},
        ]),
        (5.400, 7.200, "တကယ်ကို ကောင်းလွန်းနေတယ်။", [
            {"start": 0.05, "end": 0.72, "text": "တကယ်ကို"},
            {"start": 0.72, "end": 1.75, "text": "ကောင်းလွန်းနေတယ်။"},
        ]),
        (7.200, 8.900, "ချိစ်လေးပါ ထပ်ထည့်လိုက်တော့", [
            {"start": 0.04, "end": 0.75, "text": "ချိစ်လေးပါ"},
            {"start": 0.75, "end": 1.65, "text": "ထပ်ထည့်လိုက်တော့"},
        ]),
        (8.900, 10.600, "ပိုပြီး စားကောင်းသွားတာပေါ့။", [
            {"start": 0.05, "end": 0.55, "text": "ပိုပြီး"},
            {"start": 0.55, "end": 1.65, "text": "စားကောင်းသွားတာပေါ့။"},
        ]),
        # Real narration pause: 10.600 -> 11.300 (gap = 0.700s > 0.35s)
        (11.300, 12.800, "နောက်တစ်ဇွန်း ထပ်စားကြည့်တယ်။", [
            {"start": 0.05, "end": 0.70, "text": "နောက်တစ်ဇွန်း"},
            {"start": 0.70, "end": 1.45, "text": "ထပ်စားကြည့်တယ်။"},
        ]),
        (12.800, 14.200, "အစပ်က တဖြည်းဖြည်း", [
            {"start": 0.05, "end": 0.55, "text": "အစပ်က"},
            {"start": 0.55, "end": 1.35, "text": "တဖြည်းဖြည်း"},
        ]),
        (14.200, 16.000, "တက်လာပြီလို့ ခံစားရတယ်။", [
            {"start": 0.04, "end": 0.90, "text": "တက်လာပြီလို့"},
            {"start": 0.90, "end": 1.75, "text": "ခံစားရတယ်။"},
        ]),
    ]
    return [
        {
            "segment_id": f"seg_{i:03d}",
            "source_index": i,
            "video_start": st,
            "video_end": en,
            "audio_start": st,
            "audio_end": en,
            "audio_duration": round(en - st, 3),
            "speech_duration": round(en - st, 3),
            "text": txt,
            "word_timings": wt,
        }
        for i, (st, en, txt, wt) in enumerate(raw_specs, 1)
    ]


def test_bug2_production_shaped_segments_merge_fragments_into_full_lines():
    segments = _production_shaped_buldak_segments()
    layout = core.frame_layout("9:16", "blur", 1080, 1920, crop_top=0.12, crop_bottom=0.12)
    cw, ch, pw = int(layout["cw"]), int(layout["ch"]), int(layout["pw"])
    overlays = {
        "sub_lines": 1,
        "subtitles_font_size": 13,
        "sub_style": "cinematic",
        "sub_w": 0.92,
        "sub_font": "padauk",
        "sub_bold": True,
    }
    font_px = core.subtitle_font_px(cw, ch, 13, 1, layout)
    font_path = core.overlays_font_path(overlays)
    vis = core.build_subtitle_visuals(core.SUBTITLE_STYLE_PRESETS["cinematic"], {}, ch, font_px)
    stroke_px = int(vis.get("stroke_px", 1))
    fit_max_w = core._subtitle_fit_width_px(overlays, pw)
    pad_2 = 2 * _png_subs.png_pad_x(vis, font_px, stroke_px)
    max_ink_w = max(80, fit_max_w - pad_2)

    cues, speech_ends = core.pack_subtitle_cues_from_segments(
        segments,
        lines_per_cue=1,
        max_width_px=max_ink_w,
        font_px=font_px,
        font_path=font_path,
        stroke_px=stroke_px,
    )

    # 11 short fragments must merge into significantly fewer full-line cues
    assert len(cues) <= 7
    assert len(cues) == len(speech_ends)

    # Cue 1 ("ကျွန်တော် ဘူဒတ်") and Cue 2 ("ပြင်လိုက်တယ်။") MUST merge together (and display strips ။/၊)
    assert "ကျွန်တော် ဘူဒတ် ပြင်လိုက်တယ်" in cues[0][2]
    # "ပထမဆုံးတစ်လုပ်ကို" merges into a full sentence with "စားလိုက်တာနဲ့ အရမ်းစပ်သွားတယ်။"
    assert any(
        "ပထမဆုံးတစ်လုပ်ကို" in c_txt and "စားလိုက်တာနဲ့" in c_txt
        for _, _, c_txt in cues
    )
    # No cue ends with a dangling sentence-starting conjunction "ဒါပေမဲ့"
    for _, _, c_txt in cues:
        assert "။" not in c_txt and "၊" not in c_txt
        assert not c_txt.strip().endswith(("ဒါပေမဲ့", "ဒါပေမယ့်"))
        # No tiny 1-4 cluster orphan cue (like "ပေါ့။")
        assert len(core.myanmar_grapheme_clusters(c_txt.replace(" ", ""))) > 4
        # Every cue respects the pixel ink width budget
        assert core._measure_text_width_px(c_txt, font_px, font_path, stroke_px) <= max_ink_w

    # The >0.35s gap (10.600 -> 11.300) strictly blocks merging across the pause
    pre_gap = [c for c in cues if c[1] <= 10.61]
    post_gap = [c for c in cues if c[0] >= 11.29]
    assert pre_gap and post_gap
    assert len(pre_gap) + len(post_gap) == len(cues)
    assert pre_gap[-1][1] == pytest.approx(10.6, abs=0.02)
    assert post_gap[0][0] == pytest.approx(11.3, abs=0.02)


def test_bug2_word_timings_segment_relative_and_job_absolute_on_nonzero_segment():
    """On segment [2.0..7.0], both segment-relative (0.0..5.0) and job-absolute
    (2.0..7.0) word_timings must split at the true word boundary (~2.9s).
    """
    font_path = core.overlays_font_path({"sub_font": "padauk"})
    w1 = "ပထမစာကြောင်းတိုလေးပါ။"
    w2 = "ဒုတိယစာကြောင်းလည်းအတူတူပါပဲ။"
    seg_text = f"{w1} {w2}"
    w1_w = core._measure_text_width_px(w1, 48, font_path, 2)
    both_w = core._measure_text_width_px(seg_text, 48, font_path, 2)
    bound_w = (w1_w + both_w) // 2

    rel_wt = [
        {"start": 0.0, "end": 0.9, "text": w1},
        {"start": 0.9, "end": 5.0, "text": w2},
    ]
    abs_wt = [
        {"start": 2.0, "end": 2.9, "text": w1},
        {"start": 2.9, "end": 7.0, "text": w2},
    ]

    cues_rel, _ = core.pack_subtitle_cues_from_segments(
        [{"audio_start": 2.0, "audio_end": 7.0, "speech_duration": 5.0, "text": seg_text, "word_timings": rel_wt}],
        lines_per_cue=1,
        max_width_px=bound_w,
        font_px=48,
        font_path=font_path,
        stroke_px=2,
    )
    cues_abs, _ = core.pack_subtitle_cues_from_segments(
        [{"audio_start": 2.0, "audio_end": 7.0, "speech_duration": 5.0, "text": seg_text, "word_timings": abs_wt}],
        lines_per_cue=1,
        max_width_px=bound_w,
        font_px=48,
        font_path=font_path,
        stroke_px=2,
    )
    cues_prop, _ = core.pack_subtitle_cues_from_segments(
        [{"audio_start": 2.0, "audio_end": 7.0, "speech_duration": 5.0, "text": seg_text}],
        lines_per_cue=1,
        max_width_px=bound_w,
        font_px=48,
        font_path=font_path,
        stroke_px=2,
    )

    assert len(cues_rel) == 2 and len(cues_abs) == 2 and len(cues_prop) == 2
    assert cues_rel[0][1] == pytest.approx(2.9, abs=0.15)
    assert cues_abs[0][1] == pytest.approx(2.9, abs=0.15)
    assert cues_prop[0][1] > 3.8


def _write_test_wav(path: Path, duration_sec: float, freq: float = 330.0, rate: int = 24000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = max(1, int(round(duration_sec * rate)))
    buf = bytearray()
    for i in range(n_samples):
        t = i / float(rate)
        # Quiet lead/tail (50ms) and clear speech-level tone in middle so trim_edge_silence keeps duration
        env = 0.0 if (t < 0.04 or t > max(0.05, duration_sec - 0.04)) else 0.35
        sample = int(32767 * env * math.sin(2.0 * math.pi * freq * t))
        buf.extend(int(sample).to_bytes(2, byteorder="little", signed=True))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(bytes(buf))


@pytest.mark.asyncio
async def test_e2e_worker_mode_recap_pipeline_merges_subtitles_and_syncs_bundle_to_web(
    tmp_path, monkeypatch
):
    """Full E2E verification through run_recap_pipeline -> EdgeSmartSync.run + worker sync:
    - Real ffmpeg source video & render
    - Production-shaped planner segments + segment-relative word_timings
    - Verifies SRT, ASS, and burned subtitle_cues all share the merged packed cues
    - Verifies worker sync lands the full bundle on web_storage and bot lookup finds MP4
    """
    ffmpeg_bin = recap_pipeline.find_binary("ffmpeg")
    ffprobe_bin = recap_pipeline.find_binary("ffprobe")

    worker_storage = tmp_path / "worker_storage"
    web_storage = tmp_path / "web_storage"
    worker_storage.mkdir()
    web_storage.mkdir()

    # 1. Generate a real 12s source video via ffmpeg
    source_mp4 = tmp_path / "source_input.mp4"
    subprocess.run(
        [
            ffmpeg_bin, "-y",
            "-f", "lavfi", "-i", "testsrc=size=540x960:rate=24:duration=12",
            "-f", "lavfi", "-i", "sine=frequency=220:duration=12",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(source_mp4),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    plan_segments = [
        {"segment_id": "seg_001", "source_index": 1, "video_start": 0.0, "video_end": 1.1, "narration": "ကျွန်တော် ဘူဒတ်"},
        {"segment_id": "seg_002", "source_index": 2, "video_start": 1.1, "video_end": 2.2, "narration": "ပြင်လိုက်တယ်။"},
        {"segment_id": "seg_003", "source_index": 3, "video_start": 2.2, "video_end": 3.4, "narration": "ပထမဆုံးတစ်လုပ်ကို"},
        {"segment_id": "seg_004", "source_index": 4, "video_start": 3.4, "video_end": 5.2, "narration": "စားလိုက်တာနဲ့ အရမ်းစပ်သွားတယ်။"},
        {"segment_id": "seg_005", "source_index": 5, "video_start": 5.2, "video_end": 6.5, "narration": "ဒါပေမဲ့ အရသာက"},
        {"segment_id": "seg_006", "source_index": 6, "video_start": 6.5, "video_end": 8.3, "narration": "တကယ်ကို ကောင်းလွန်းနေတယ်။"},
        {"segment_id": "seg_007", "source_index": 7, "video_start": 8.3, "video_end": 9.8, "narration": "ချိစ်လေးပါ ထပ်ထည့်လိုက်တော့"},
        {"segment_id": "seg_008", "source_index": 8, "video_start": 9.8, "video_end": 11.5, "narration": "ပိုပြီး စားကောင်းသွားတာပေါ့။"},
    ]

    def _fake_planner_build(self, *, source, job_dir, source_title="", reuse=True, **kwargs):
        out_p = Path(job_dir) / "smart_sync_plan.json"
        plan_obj = {
            "title": "Buldak E2E Test",
            "story_style": "pov",
            "segments": plan_segments,
            "api_usage": {"requests": 1, "input_tokens": 100, "output_tokens": 80, "model": "gemini-2.5-flash"},
            "stage_timings": {"stt": 0.1, "plan": 0.1},
        }
        out_p.write_text(json.dumps(plan_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        return out_p, plan_obj

    monkeypatch.setattr(recap_pipeline.RecapPlanBuilder, "build", _fake_planner_build)
    # Bypass curated persona so EdgeTTSEngine.generate_channel99_voice runs and populates segment-relative word_timings
    monkeypatch.setattr("app.services.curated_voices.get_curated_voice", lambda _slug: None)

    def _fake_edge_generate(self, text: str, voice_name: str | None = None) -> Path:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        out_wav = self.work_dir / f"tts_{ abs(hash(text)) & 0xFFFFFFFF:08x}.wav"
        dur = max(0.85, min(2.0, 0.055 * max(8, len(text))))
        _write_test_wav(out_wav, dur)
        words = [w for w in text.split(" ") if w] or [text]
        step = dur / len(words)
        self._tls.last_word_timings = [
            {"start": round(i * step, 3), "end": round((i + 1) * step, 3), "text": w}
            for i, w in enumerate(words)
        ]
        return out_wav

    monkeypatch.setattr(core.EdgeTTSEngine, "generate_channel99_voice", _fake_edge_generate)

    jid = "rc_e2e_worker_20261122"
    worker_job_dir = worker_storage / "recap_output" / jid / "workspace"
    res = recap_pipeline.run_recap_pipeline(
        source_video_path=source_mp4,
        job_dir=worker_job_dir,
        voice="my-MM-ThihaNeural",
        tts_engine="edge_tts",
        voice_speed="+12%",
        narrative_style="pov",
        output_ratio="9:16",
        frame_fill="blur",
        crop_top=0.12,
        crop_bottom=0.12,
        mirror_video=True,
        zoom_factor=1.1,
        blur_subtitles=False,
        burn_subtitles=True,
        sub_lines=1,
        sub_font_size=13,
        sub_style="cinematic",
        sub_font="padauk",
        sub_w=0.92,
        protection_level="standard",
    )

    assert res.get("final_video") and Path(res["final_video"]).is_file()
    assert res.get("audio") and Path(res["audio"]).is_file()
    assert res.get("subtitles_srt") and Path(res["subtitles_srt"]).is_file()
    assert res.get("subtitles_ass") and Path(res["subtitles_ass"]).is_file()

    # Verify Bug 2: SRT, ASS, and burned subtitle_cues all reflect the packed cues (< 8 input segments)
    srt_cues = _png_subs.parse_srt_cues(Path(res["subtitles_srt"]))
    assert 2 <= len(srt_cues) <= 5, f"Expected 8 segments to pack into 2..5 cues, got {len(srt_cues)}: {srt_cues}"
    assert "ကျွန်တော် ဘူဒတ် ပြင်လိုက်တယ်" in srt_cues[0][2]
    assert all("။" not in c[2] and "၊" not in c[2] for c in srt_cues)

    plan_edge = json.loads(
        (worker_job_dir / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json").read_text(encoding="utf-8")
    )
    burned_cues = plan_edge["production_overlays"]["subtitle_cues"]
    assert len(burned_cues) == len(srt_cues)
    assert [c[2] for c in burned_cues] == [c[2] for c in srt_cues]

    ass_text = Path(res["subtitles_ass"]).read_text(encoding="utf-8")
    ass_dialogues = [ln for ln in ass_text.splitlines() if ln.startswith("Dialogue:")]
    assert len(ass_dialogues) == len(srt_cues)
    assert all("။" not in ln and "၊" not in ln for ln in ass_dialogues)

    # Verify Bug 1: build script on worker, upload bundle to web_storage, verify web & bot resolution
    worker_outer_dir = worker_storage / "recap_output" / jid
    script_path = api_mod._build_clean_recap_script_file(
        worker_outer_dir, jid, "pov", res["subtitles_srt"]
    )
    assert script_path and "။" in Path(script_path).read_text(encoding="utf-8")
    bundle = collect_recap_bundle_files(
        worker_outer_dir,
        video_path=res["final_video"],
        audio_path=res["audio"],
        srt_path=res["subtitles_srt"],
        ass_path=res["subtitles_ass"],
        script_path=script_path,
    )
    assert len(bundle) >= 6

    engine, session_maker = await _make_isolated_db()
    try:
        monkeypatch.setattr(settings, "instance_primary", False)
        monkeypatch.setattr(settings, "worker_token", TEST_WORKER_TOKEN)
        monkeypatch.setattr(api_mod, "DISK_HEADROOM_MIN_FREE_BYTES", 0)
        async with session_maker() as s:
            await _seed_recap_job(s, job_id=jid, status=JobStatus.RUNNING.value)

        class _Resp:
            def __init__(self, data):
                self.status_code = 200
                self.is_success = True
                self._data = data
                self.text = json.dumps(data)

            def json(self):
                return self._data

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False

            async def post(self, url, *, files=None, data=None, headers=None):
                fname, fh, _m = files["file"]
                raw = fh.read()
                prev = settings.storage_root
                settings.storage_root = web_storage
                try:
                    async with session_maker() as ws:
                        out = await api_mod.upload_worker_recap_file(
                            jid,
                            file=_upload_file(fname, raw),
                            rel_path=data["rel_path"],
                            role=data["role"],
                            auth_token=headers["X-Worker-Token"],
                            db=ws,
                        )
                    return _Resp(out)
                finally:
                    settings.storage_root = prev

        monkeypatch.setattr("httpx.AsyncClient", _Client)
        sync_res = await sync_recap_bundle_to_cloud(jid, worker_outer_dir, bundle)
        assert sync_res["synced"] is True
        assert sync_res["skipped"] is False

        # Bot-facing lookup on web_storage
        settings.storage_root = web_storage
        web_video_path = sync_res["remote_paths"]["video"]
        for ftype in ("video", "audio", "srt", "script"):
            found = api_mod.resolve_recap_deliverable_path(jid, ftype, result_path=web_video_path)
            assert found is not None and Path(found).is_file(), f"Web missing {ftype}"
            assert Path(found).is_relative_to(web_storage)
    finally:
        await engine.dispose()
