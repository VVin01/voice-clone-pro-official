"""Regression tests for the 2026-11-18 Full Audit & Production Upgrade (P0-P3)."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_recovery_skips_parked_script_ready_and_fails_stale_recap(
    db_session, monkeypatch
):
    from app.models.enums import JobStatus, JobType
    from app.models.schema import AuditLog, Job, User
    from app.services import recovery
    from sqlalchemy import select

    user = User(id=991801, first_name="AuditUser")
    db_session.add(user)
    await db_session.commit()

    stale_ts = datetime.now(timezone.utc) - timedelta(hours=2)
    parked = Job(
        id="rc_parked_test_1",
        user_id=user.id,
        job_type=JobType.RECAP.value,
        engine="recap",
        status=JobStatus.RUNNING.value,
        started_at=stale_ts,
        created_at=stale_ts,
        recap_config=json.dumps({
            "script_ready": True,
            "script_ready_at": datetime.now(timezone.utc).isoformat(),
        }),
    )
    stranded = Job(
        id="rc_stranded_test_2",
        user_id=user.id,
        job_type=JobType.RECAP.value,
        engine="recap",
        status=JobStatus.RUNNING.value,
        started_at=stale_ts,
        created_at=stale_ts,
        recap_config=json.dumps({"script_ready": False}),
    )
    db_session.add_all([parked, stranded])
    await db_session.commit()

    failed_ids = []

    async def fake_fail(job_id: str, user_id: int, state: dict) -> None:
        failed_ids.append((job_id, user_id, state))

    monkeypatch.setattr("app.webapp.api._fail_stranded_recap_job", fake_fail)

    recovered = await recovery.recover_stale_running_jobs(
        db_session,
        stale_timeout_seconds=300,
        recap_stale_timeout_seconds=300,
    )

    refreshed_parked = await db_session.get(Job, "rc_parked_test_1")
    refreshed_stranded = await db_session.get(Job, "rc_stranded_test_2")
    assert refreshed_parked.status == JobStatus.RUNNING.value
    assert refreshed_stranded.status == JobStatus.FAILED.value
    assert "rc_stranded_test_2" in recovered
    assert "rc_parked_test_1" not in recovered
    assert any(jid == "rc_stranded_test_2" for jid, *_ in failed_ids)

    logs = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.target_id == "rc_stranded_test_2")
        )
    ).scalars().all()
    assert len(logs) == 1
    assert logs[0].action == "recap.stale_recovery"


def test_whisper_transcribe_groq_retains_temp_audio_across_keys(
    tmp_path, monkeypatch
):
    from app.services.recap_engine.services.whisper_service import (
        WhisperTimestampService,
    )

    src_video = tmp_path / "sample.mkv"
    src_video.write_bytes(b"\x00" * 512)
    temp_audio = tmp_path / "sample_groq_tmp.mp3"

    svc = WhisperTimestampService.__new__(WhisperTimestampService)
    svc.language = "auto"
    svc._log_msg = lambda *_a, **_kw: None

    monkeypatch.setenv("GROQ_API_KEYS", "gsk_test_one,gsk_test_two")

    class FakeRotation:
        def key_id(self, key: str) -> str:
            return key[-4:]

        def health_for(self, _prov):
            class H:
                def order(self, items):
                    return items

                def mark_success(self, *_a, **_kw):
                    pass

                def mark_failure(self, *_a, **_kw):
                    pass

                def record_usage(self, *_a, **_kw):
                    pass

            return H()

    import app.services.recap_engine.services.whisper_service as ws_mod
    import requests
    import subprocess

    monkeypatch.setattr(ws_mod, "_key_rotation", FakeRotation(), raising=False)
    import importlib
    monkeypatch.setattr(
        importlib.import_module("app.config.settings"),
        "get_groq_whisper_model",
        lambda: "whisper-large-v3-turbo",
    )

    def fake_subproc_run(cmd, **_kwargs):
        out_file = Path(cmd[-1])
        out_file.write_bytes(b"ID3" + b"\x00" * 128)
        # Keep handle of exact temp_audio name pattern created
        temp_audio.unlink(missing_ok=True)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_subproc_run)

    seen_files_during_keys = []

    def fake_post(_url, headers=None, files=None, **_kwargs):
        file_obj = files["file"][1]
        sent_path = Path(file_obj.name)
        seen_files_during_keys.append((headers["Authorization"], sent_path.is_file()))
        if headers["Authorization"].endswith("gsk_test_one"):
            return SimpleNamespace(status_code=429, text="rate limited")
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"segments": [{"start": 0.0, "end": 1.2, "text": "hello"}]},
        )

    monkeypatch.setattr(requests, "post", fake_post)

    res = svc._transcribe_groq(str(src_video), media_duration=2.0)
    assert res is not None and res["segments"][0]["text"] == "hello"
    assert seen_files_during_keys == [
        ("Bearer gsk_test_one", True),
        ("Bearer gsk_test_two", True),
    ]
    # Cleaned up once _transcribe_groq exits
    remaining = list(tmp_path.glob("*groq*"))
    assert remaining == []


@pytest.mark.asyncio
async def test_rate_limit_canonical_bucketing_and_put_enforcement():
    from app.webapp.rate_limit import RateLimitMiddleware

    mw = RateLimitMiddleware(
        app=lambda s, r, sd: None,
        limits={"/api/jobs/{job_id}/cancel": (1, 60), "/api/user/preferences": (1, 60)},
    )
    m1 = mw._match_limit("/api/jobs/job_alpha/cancel")
    m2 = mw._match_limit("/api/jobs/job_beta/cancel")
    assert m1 is not None and m2 is not None
    assert m1[0] == m2[0] == "/api/jobs/{job_id}/cancel"

    def _make_req(method: str, path: str) -> Request:
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "client": ("10.1.2.3", 54321),
        }
        return Request(scope)

    async def _ok(_req):
        return JSONResponse({"ok": True})

    r1 = await mw.dispatch(_make_req("PUT", "/api/user/preferences"), _ok)
    r2 = await mw.dispatch(_make_req("PUT", "/api/user/preferences"), _ok)
    assert r1.status_code == 200
    assert r2.status_code == 429


@pytest.mark.asyncio
async def test_admin_fleet_status_reads_paused_bot_setting(db_session):
    from app.models.schema import BotSetting, User, utc_now
    from app.webapp.api import get_admin_fleet_status

    admin = User(id=991802, first_name="AdminUser")
    db_session.add(admin)
    db_session.add_all(
        [
            BotSetting(key="worker_laptop_status", value="PAUSED"),
            BotSetting(key="worker_laptop_heartbeat", value=utc_now().isoformat()),
            BotSetting(key="worker_laptop_host", value="worker.internal"),
        ]
    )
    await db_session.commit()

    payload = await get_admin_fleet_status(admin=admin, db=db_session)
    assert payload["vps_worker"]["status_code"] == "PAUSED"
    assert payload["vps_worker"]["host"] == "worker.internal"


def test_frontend_p2_and_assets_p3_contracts():
    index_html = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
    i18n_js = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
    app_js = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    home_idx = index_html.index('id="tab-home"')
    voice_idx = index_html.index('id="tab-voice"')
    err_idx = index_html.index('id="home-load-error"')
    assert home_idx < err_idx < voice_idx

    assert 'data-hub-filter="TTS"' in index_html
    assert 'id="adm-vps-host-label">(VPS Worker)<' in index_html
    assert "212.28.177.252" not in index_html
    assert '<span class="acc-num" id="kpay-num">—</span>' in index_html
    assert '<span class="acc-num" id="wave-num">—</span>' in index_html
    assert index_html.count("?v=20261125_admin_r1") == 5

    my_part = i18n_js.split("    my: {", 1)[1].split("    en: {", 1)[0]
    en_part = i18n_js.split("    en: {", 1)[1]
    my_keys = set(re.findall(r"(?m)^        (\w+):", my_part))
    en_keys = set(re.findall(r"(?m)^        (\w+):", en_part))
    assert my_keys == en_keys
    for k in (
        "history_filter_tts",
        "adm_fleet_vps_host_default",
        "voice_cancel_btn",
        "tts_cancel_btn",
        "copy_unavailable_placeholder",
    ):
        assert k in my_keys

    for sym in (
        "cancelVoiceJob",
        "cancelTtsJob",
        "pauseInactiveTabMedia",
        "sanitizeCssMediaUrl",
        "getAuthHeaders(false)",
        "ctx.close()",
    ):
        assert sym in app_js

    from app.services.curated_voices import PERSONA_REFERENCE_SOURCES, get_persona_reference

    assets_ref_dir = ROOT / "app/assets/audio/references"
    for slug in PERSONA_REFERENCE_SOURCES:
        assert (assets_ref_dir / f"{slug}.wav").is_file()
        assert (assets_ref_dir / f"{slug}.txt").is_file()
        ref = get_persona_reference(slug)
        assert ref is not None
        wav_path, txt = ref
        assert "assets" in wav_path and Path(wav_path).is_file() and txt.strip()

    assert "fonts-noto-extra" not in dockerfile
    for pkg in ("libraqm0", "fonts-sil-padauk", "fonts-noto-core"):
        assert pkg in dockerfile
    for entry in ("tests/", ".pytest_cache/", "docs/", ".github/"):
        assert entry in dockerignore
