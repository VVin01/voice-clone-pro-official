"""SCRIPT_READY persistence, restart restore and 24h TTL expiry (2026-11-03).

Before this change a recap job parked at SCRIPT_READY for script
review existed only in the in-memory registry: a restart lost the
parked state, the queue worker re-ran the script phase (re-download,
re-transcription, re-translation) and overwrote the plan file holding
the user's edits, and a job abandoned in review held its concurrency
slot and its charged credit forever.

Covered here:
(a) parking persists phase="render" + script_ready + source_path +
    segments_count + script_ready_at into Job.recap_config;
(b) restore rebuilds a SCRIPT_READY registry entry from the row
    without invoking the background runner or any pipeline stage;
(c) expiry past the 24h TTL marks the job FAILED, frees the slot and
    refunds exactly once (a second run refunds nothing more);
(d) a job parked under the TTL is untouched;
(e) the admin history routes sit behind require_admin (403 for a
    non-admin) and keep working for an admin.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.session as session_mod
from app.config import settings
from app.db.base import Base
from app.models.enums import JobStatus, JobType
from app.models.schema import Job, User
from app.webapp import api as api_mod


# ── helpers ──────────────────────────────────────────────────────────────

async def _open_store(tmp_path, monkeypatch):
    """Point the API helpers' own session factory at a real file DB."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/script_ready.db", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    monkeypatch.setattr(settings, "admin_ids_raw", "424242")
    return engine, maker


def _recap_state(**over):
    cfg = api_mod.RecapConfigRequest(
        url="https://example.com/movie",
        title="Parked Movie",
        voice_slug="mahnin",
        script_review=True,
    )
    state = {
        "config": cfg.model_dump(),
        "credit_deducted": True,
        "credits_charged": 1,
        "is_vip": False,
        "phase": "script",
        "credits_estimated": None,
        "credit_refunded": False,
        "credits_refunded": 0,
    }
    state.update(over)
    return state


def _job(job_id, user_id, state, status=JobStatus.RUNNING.value):
    return Job(
        id=job_id,
        user_id=user_id,
        chat_id=user_id,
        status=status,
        job_type=JobType.RECAP.value,
        engine="recap",
        preset_voice="mahnin",
        input_text="Parked Movie",
        source_audio_path="https://example.com/movie",
        recap_config=json.dumps(state),
    )


def _parked_state(hours_ago, **over):
    base = {
        "phase": "render",
        "script_ready": True,
        "source_path": "/data/source.mp4",
        "segments_count": 7,
        "script_ready_at": (
            datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        ).isoformat(),
    }
    base.update(over)
    return _recap_state(**base)


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    for jid in ("rc_sr_a", "rc_sr_b", "rc_sr_c", "rc_sr_d"):
        api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)


# ── (a) parking persists the script_ready fields ─────────────────────────

@pytest.mark.asyncio
async def test_a_parking_persists_script_ready_fields(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 910101
        async with maker() as s:
            s.add(User(id=uid, recap_credits=4))
            s.add(_job("rc_sr_a", uid, _recap_state()))
            await s.commit()

        parked_at = datetime.now(timezone.utc).isoformat()
        entry = {
            "id": "rc_sr_a",
            "source_path": "/data/source.mp4",
            "segments_count": 12,
            "script_ready_at": parked_at,
        }
        await api_mod._persist_recap_script_ready(entry)

        async with maker() as s:
            job = await s.get(Job, "rc_sr_a")
            state = json.loads(job.recap_config)
        assert state["phase"] == "render"
        assert state["script_ready"] is True
        assert state["source_path"] == "/data/source.mp4"
        assert state["segments_count"] == 12
        assert state["script_ready_at"] == parked_at
        # Pre-existing persisted keys are untouched.
        assert state["config"]["script_review"] is True
        assert state["credits_charged"] == 1
        assert state["credit_deducted"] is True
    finally:
        await engine.dispose()


# ── (b) restore rebuilds the parked entry without re-running stages ─────

@pytest.mark.asyncio
async def test_b_restore_rebuilds_entry_without_rerunning(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 910102
        job = _job("rc_sr_b", uid, _parked_state(hours_ago=1))
        async with maker() as s:
            s.add(User(id=uid, recap_credits=4))
            s.add(job)
            await s.commit()

        # The background runner (which downloads, transcribes and
        # translates) must never be invoked by a restore.
        runner = AsyncMock(side_effect=AssertionError("restore re-ran the job"))
        monkeypatch.setattr(api_mod, "_run_recap_background_job", runner)

        # Pure builder first.
        entry = api_mod._restore_script_ready_from_db(job)
        assert entry is not None
        assert entry["status"] == "SCRIPT_READY"
        assert entry["source_path"] == "/data/source.mp4"
        assert entry["segments_count"] == 7
        assert entry["req_snapshot"]["script_review"] is True
        assert entry["credits_charged"] == 1
        assert entry["credit_deducted"] is True

        # Then the startup sweep lands it in the registry.
        api_mod.RECAP_JOBS_REGISTRY.pop("rc_sr_b", None)
        restored = await api_mod.restore_script_ready_jobs()
        assert restored == 1
        reg = api_mod.RECAP_JOBS_REGISTRY["rc_sr_b"]
        assert reg["status"] == "SCRIPT_READY"
        assert reg["segments_count"] == 7
        runner.assert_not_called()

        # A non-parked row restores nothing.
        plain = _job("rc_sr_plain", uid, _recap_state())
        assert api_mod._restore_script_ready_from_db(plain) is None
    finally:
        await engine.dispose()


# ── (c) expiry past the TTL: FAILED + slot freed + refund exactly once ──

@pytest.mark.asyncio
async def test_c_expire_over_ttl_refunds_once_and_frees_slot(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 910103
        state = _parked_state(hours_ago=25)
        async with maker() as s:
            s.add(User(id=uid, recap_credits=4))  # submit already took 1 of 5
            s.add(_job("rc_sr_c", uid, state))
            await s.commit()

        # The live registry holds the parked entry, as in production.
        api_mod.RECAP_JOBS_REGISTRY["rc_sr_c"] = {
            "id": "rc_sr_c",
            "user_id": uid,
            "status": "SCRIPT_READY",
            "credits_charged": 1,
            "credit_deducted": True,
            "credit_refunded": False,
            "credits_refunded": 0,
            "script_ready_at": state["script_ready_at"],
        }
        async with maker() as s:
            assert await api_mod._recap_active_job_count(s, uid) == 1

        expired = await api_mod.expire_abandoned_script_ready_jobs()
        assert expired == 1

        async with maker() as s:
            job = await s.get(Job, "rc_sr_c")
            assert job.status == JobStatus.FAILED.value
            stored = json.loads(job.recap_config)
            assert stored["script_ready"] is False
            assert stored["credit_refunded"] is True
            assert stored["credits_refunded"] == 1
            user = await s.get(User, uid)
            assert user.recap_credits == 5  # exactly whole again
            # Slot freed on both branches of the active-job counter.
            assert await api_mod._recap_active_job_count(s, uid) == 0
        assert api_mod.RECAP_JOBS_REGISTRY["rc_sr_c"]["status"] == "FAILED"

        # Second run: nothing left to expire, no second refund.
        assert await api_mod.expire_abandoned_script_ready_jobs() == 0
        async with maker() as s:
            user = await s.get(User, uid)
            assert user.recap_credits == 5
    finally:
        await engine.dispose()


# ── (d) a job parked under the TTL is untouched ──────────────────────────

@pytest.mark.asyncio
async def test_d_expire_under_ttl_leaves_job_untouched(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 910104
        async with maker() as s:
            s.add(User(id=uid, recap_credits=4))
            s.add(_job("rc_sr_d", uid, _parked_state(hours_ago=1)))
            await s.commit()

        assert await api_mod.expire_abandoned_script_ready_jobs() == 0

        async with maker() as s:
            job = await s.get(Job, "rc_sr_d")
            assert job.status == JobStatus.RUNNING.value
            stored = json.loads(job.recap_config)
            assert stored["script_ready"] is True
            assert stored["credit_refunded"] is False
            user = await s.get(User, uid)
            assert user.recap_credits == 4
    finally:
        await engine.dispose()


# ── (e) admin history routes: require_admin gate + admin access ─────────

# Assembled at runtime so this source never carries a literal token-shaped string.
_TEST_BOT_TOKEN = "123456789:" + "TEST" + "x" * 31


def _signed_init_data(user_id: int, token: str = _TEST_BOT_TOKEN) -> str:
    fields = {
        "user": json.dumps({"id": user_id, "first_name": "T"}),
        "query_id": "AAE",
        "auth_date": str(int(time.time())),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode({**fields, "hash": digest})


def _route_dependant(path: str, method: str):
    for route in api_mod.api_router.routes:
        if getattr(route, "path", None) == path and method in getattr(
            route, "methods", set()
        ):
            return route.dependant
    raise AssertionError(f"route {method} {path} not found")


def _dependant_has(dependant, target) -> bool:
    for dep in dependant.dependencies:
        if dep.call is target or _dependant_has(dep.dependant, target):
            return True
    return False


@pytest.mark.asyncio
async def test_e_admin_history_routes_require_admin(db_session, monkeypatch):
    admin_id, user_id = 920101, 920102
    monkeypatch.setattr(settings, "admin_ids_raw", str(admin_id))
    monkeypatch.setattr(settings, "bot_token", _TEST_BOT_TOKEN)

    # Both routes are wired to require_admin, like the other admin routes.
    assert _dependant_has(
        _route_dependant("/api/admin/history", "GET"), api_mod.require_admin
    )
    assert _dependant_has(
        _route_dependant("/api/admin/history/purge", "POST"), api_mod.require_admin
    )

    admin = User(id=admin_id, first_name="Admin")
    plain = User(id=user_id, first_name="Plain")
    db_session.add_all([admin, plain])
    await db_session.commit()

    # require_admin itself: a verified non-admin gets 403, the admin passes.
    with pytest.raises(HTTPException) as exc:
        api_mod.require_admin(
            x_telegram_init_data=_signed_init_data(user_id), user=plain
        )
    assert exc.value.status_code == 403
    assert (
        api_mod.require_admin(
            x_telegram_init_data=_signed_init_data(admin_id), user=admin
        )
        is admin
    )

    # The endpoints keep working for an admin.
    res = await api_mod.get_admin_global_history(user=admin, db=db_session)
    assert res["total_count"] == 0
    purged = await api_mod.purge_admin_history(
        api_mod.AdminPurgeHistoryRequest(mode="single", job_id="missing"),
        user=admin,
        db=db_session,
    )
    assert purged["success"] is True
    assert purged["purged_count"] == 0
