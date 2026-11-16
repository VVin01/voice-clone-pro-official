"""Part B0 (2026-11-14, r150) — Recap stage_timings admin telemetry aggregation.

Covers:
- Admin-only endpoint ``GET /api/admin/stage-timings`` (401 without auth, 403 for non-admin).
- Aggregation over the last 100 completed RECAP jobs (ignoring VOICE_CLONE/TTS and non-SUCCESS rows).
- Per-stage ``p50``, ``p95``, ``max``, ``count`` over ``download``, ``transcribe``,
  ``translate``, ``tts``, ``sync``, ``png``, ``overlay``, ``mux`` plus ``total``.
- In-memory TTL cache (``refresh=0`` cached hit, ``refresh=1`` bypass, TTL expiry).
- Admin Overview card, refresh button, MY/EN i18n parity, and ``r150`` cache token.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

import app.webapp.api as api
from app.config import settings
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Job, User
from app.services.ledger import get_or_create_user

REPO = Path(__file__).resolve().parents[2]
INDEX_HTML = (REPO / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (REPO / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")


async def _mk_user(session, user_id: int, *, is_admin: bool = False) -> User:
    user, _ = await get_or_create_user(session, user_id, username=f"b0_{user_id}")
    user.account_category = AccountCategory.PURCHASED.value
    if is_admin:
        cur = str(settings.admin_ids_raw or "").strip()
        settings.admin_ids_raw = f"{cur},{user_id}" if cur else str(user_id)
    await session.commit()
    return user


def _make_recap_job(
    job_id: str,
    user_id: int,
    *,
    status: str = JobStatus.SUCCESS.value,
    stage_timings: dict | None = None,
    raw_cfg: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    created_at: datetime | None = None,
    job_type: str = JobType.RECAP.value,
    engine: str = "recap",
) -> Job:
    cfg = raw_cfg
    if cfg is None and stage_timings is not None:
        cfg = json.dumps({"stage_timings": stage_timings})
    return Job(
        id=job_id,
        user_id=user_id,
        job_type=job_type,
        engine=engine,
        status=status,
        input_text=f"Job {job_id}",
        recap_config=cfg,
        started_at=started_at,
        finished_at=finished_at,
        created_at=created_at or datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_b0_endpoint_requires_admin(db_session, monkeypatch):
    from app.db.session import get_db_session
    from app.webapp.server import webapp_app as app

    non_admin = await _mk_user(db_session, 91001, is_admin=False)
    admin = await _mk_user(db_session, 91002, is_admin=True)

    async def _override_db():
        yield db_session

    app.dependency_overrides[get_db_session] = _override_db
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r_unauth = await client.get("/api/admin/stage-timings")
            assert r_unauth.status_code in (401, 403)

            monkeypatch.setattr(api, "parse_and_verify_init_data", lambda _raw: {"id": non_admin.id})
            monkeypatch.setattr(api, "init_data_auth_date", lambda _raw: time.time())
            r_user = await client.get(
                "/api/admin/stage-timings",
                headers={"X-Telegram-Init-Data": "signed-user"},
            )
            assert r_user.status_code == 403

            monkeypatch.setattr(api, "parse_and_verify_init_data", lambda _raw: {"id": admin.id})
            r_adm = await client.get(
                "/api/admin/stage-timings",
                headers={"X-Telegram-Init-Data": "signed-admin"},
            )
            assert r_adm.status_code == 200
            data = r_adm.json()
            assert set(data["stages"]) == set(api._RECAP_STAGE_TIMINGS_STAGES)
    finally:
        app.dependency_overrides.clear()
        api.clear_admin_stage_timings_cache()


@pytest.mark.asyncio
async def test_b0_aggregates_last_100_completed_recap_jobs_and_ignores_noise(db_session):
    api.clear_admin_stage_timings_cache()
    admin = await _mk_user(db_session, 91010, is_admin=True)
    user = await _mk_user(db_session, 91011, is_admin=False)
    t0 = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)

    # Seed 105 completed recap jobs; oldest 5 (idx 0..4) carry huge outlier values (9999s)
    # and MUST fall outside the last-100 window.
    for i in range(105):
        fin = t0 + timedelta(minutes=i)
        if i < 5:
            st = {"download": 9999.0, "transcribe": 9999.0, "tts": 9999.0}
            sta = fin - timedelta(seconds=9999)
        else:
            # idx 5..104 -> 100 values: i - 4 in 1..100
            val = float(i - 4)
            st = {
                "download": val,
                "transcribe": val * 2,
                "translate": 10.0,
                "tts": val * 3,
                "sync": 5.0,
                "png": 2.5,
                "overlay": 15.0,
                "mux": 1.5,
            }
            sta = fin - timedelta(seconds=val + 50.0)
        db_session.add(
            _make_recap_job(
                f"rc_b0_{i:03d}",
                user.id,
                stage_timings=st,
                started_at=sta,
                finished_at=fin,
                created_at=fin,
            )
        )

    # Add non-SUCCESS and non-RECAP jobs with massive values — must all be excluded
    latest = t0 + timedelta(minutes=200)
    db_session.add(
        _make_recap_job(
            "rc_b0_failed",
            user.id,
            status=JobStatus.FAILED.value,
            stage_timings={"download": 8888.0},
            finished_at=latest,
            created_at=latest,
        )
    )
    db_session.add(
        _make_recap_job(
            "vc_b0_clone",
            user.id,
            status=JobStatus.SUCCESS.value,
            job_type=JobType.VOICE_CLONE.value,
            engine="voxcpm",
            stage_timings={"download": 7777.0},
            finished_at=latest,
            created_at=latest,
        )
    )
    # Legacy malformed JSON row in window shouldn't crash; wait: don't make it displace valid ones unnecessarily, test malformed separately below
    await db_session.commit()

    payload = await api.get_admin_stage_timings(refresh=1, limit=100, admin=admin, db=db_session)
    assert payload["sample_limit"] == 100
    assert payload["jobs_scanned"] == 100
    assert payload["jobs_with_timings"] == 100
    assert payload["cached"] is False

    dl = payload["stages"]["download"]
    assert dl["count"] == 100
    assert dl["max"] == 100.0  # 9999 outliers were excluded by LIMIT 100
    assert dl["p50"] == pytest.approx(50.5, abs=0.02)
    assert dl["p95"] == pytest.approx(95.05, abs=0.05)

    tts = payload["stages"]["tts"]
    assert tts["max"] == 300.0
    assert tts["p50"] == pytest.approx(151.5, abs=0.05)

    tot = payload["total"]
    assert tot["count"] == 100
    assert tot["max"] == 150.0
    assert tot["p50"] == pytest.approx(100.5, abs=0.05)
    assert tot["p95"] == pytest.approx(145.05, abs=0.05)


@pytest.mark.asyncio
async def test_b0_handles_legacy_malformed_and_fallback_totals(db_session):
    api.clear_admin_stage_timings_cache()
    admin = await _mk_user(db_session, 91020, is_admin=True)
    user = await _mk_user(db_session, 91021, is_admin=False)

    # 1) Legacy row without stage_timings & naive started/finished
    db_session.add(
        _make_recap_job(
            "rc_b0_legacy",
            user.id,
            raw_cfg="not-json",
            started_at=datetime(2026, 11, 14, 10, 0, 0),
            finished_at=datetime(2026, 11, 14, 10, 1, 0),  # 60s
        )
    )
    # 2) Booleans/negatives ignored; total falls back to sum of valid stages (4 + 6 = 10s)
    db_session.add(
        _make_recap_job(
            "rc_b0_clean",
            user.id,
            stage_timings={"download": 4.0, "mux": 6.0, "tts": True, "sync": -3.0},
            started_at=None,
            finished_at=None,
        )
    )
    await db_session.commit()

    payload = await api.get_admin_stage_timings(refresh=1, limit=100, admin=admin, db=db_session)
    assert payload["jobs_scanned"] == 2
    assert payload["jobs_with_timings"] == 1
    assert payload["stages"]["download"] == {"count": 1, "p50": 4.0, "p95": 4.0, "max": 4.0}
    assert payload["stages"]["mux"] == {"count": 1, "p50": 6.0, "p95": 6.0, "max": 6.0}
    assert payload["stages"]["tts"] == {"count": 0, "p50": None, "p95": None, "max": None}
    assert payload["stages"]["sync"] == {"count": 0, "p50": None, "p95": None, "max": None}
    assert payload["total"] == {"count": 2, "p50": 35.0, "p95": 57.5, "max": 60.0}


@pytest.mark.asyncio
async def test_b0_cache_hit_and_on_demand_refresh(db_session, monkeypatch):
    api.clear_admin_stage_timings_cache()
    admin = await _mk_user(db_session, 91030, is_admin=True)
    user = await _mk_user(db_session, 91031, is_admin=False)

    db_session.add(_make_recap_job("rc_b0_c1", user.id, stage_timings={"mux": 2.0}))
    await db_session.commit()

    first = await api.get_admin_stage_timings(refresh=0, limit=100, admin=admin, db=db_session)
    assert first["cached"] is False
    assert first["jobs_scanned"] == 1

    # Add a second completed job; refresh=0 still serves the cached snapshot
    db_session.add(_make_recap_job("rc_b0_c2", user.id, stage_timings={"mux": 8.0}))
    await db_session.commit()

    second = await api.get_admin_stage_timings(refresh=0, limit=100, admin=admin, db=db_session)
    assert second["cached"] is True
    assert second["jobs_scanned"] == 1

    # refresh=1 bypasses cache and picks up the new job
    refreshed = await api.get_admin_stage_timings(refresh=1, limit=100, admin=admin, db=db_session)
    assert refreshed["cached"] is False
    assert refreshed["jobs_scanned"] == 2
    assert refreshed["stages"]["mux"]["max"] == 8.0

    # Advancing monotonic clock past TTL triggers fresh recompute even when refresh=0
    future = time.monotonic() + api._STAGE_TIMINGS_CACHE_TTL_SECONDS + 5.0
    monkeypatch.setattr(api.time, "monotonic", lambda: future)
    expired = await api.get_admin_stage_timings(refresh=0, limit=100, admin=admin, db=db_session)
    assert expired["cached"] is False
    assert expired["jobs_scanned"] == 2


def test_b0_ui_i18n_and_cache_token():
    assert 'id="admin-stage-timings-card"' in INDEX_HTML
    assert 'id="adm-stage-timings-refresh-btn"' in INDEX_HTML
    assert 'onclick="loadAdminStageTimings(true)"' in INDEX_HTML
    assert '"/api/admin/stage-timings"' in APP_JS
    assert "loadAdminStageTimings(false)" in APP_JS
    assert "syncTabAdminLifecycle(tabId)" in APP_JS
    assert "syncTabAdminLifecycle(targetTab)" in APP_JS
    assert INDEX_HTML.count("?v=20261116_r156") == 4
    assert "20261114_r149" not in INDEX_HTML

    # Verify all new B0 keys exist cleanly in BOTH my and en locales
    proc = subprocess.run(
        [
            "node",
            "-e",
            """
const vm = require("vm");
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const ctx = { window: {}, document: {}, localStorage: { getItem: () => null, setItem: () => {} } };
vm.runInNewContext(src + "; globalThis.__tr = translations;", ctx);
const keys = [
  "adm_stage_timings_title", "adm_stage_timings_meta", "adm_stage_timings_cached",
  "adm_stage_timings_total", "adm_stage_timings_col_stage", "adm_stage_timings_col_p50",
  "adm_stage_timings_col_p95", "adm_stage_timings_col_max", "adm_stage_timings_empty",
  "adm_stage_lbl_download", "adm_stage_lbl_transcribe", "adm_stage_lbl_translate",
  "adm_stage_lbl_tts", "adm_stage_lbl_sync", "adm_stage_lbl_png",
  "adm_stage_lbl_overlay", "adm_stage_lbl_mux"
];
for (const k of keys) {
  if (!ctx.__tr.my[k] || !ctx.__tr.en[k]) process.exit(2);
}
console.log(keys.length);
""",
            str(REPO / "app/webapp/static/js/i18n.js"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert int(proc.stdout.strip()) == 17
