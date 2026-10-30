"""Admin production tools (branch feat-admin-production-tools-2026-10-31).

Covers the gaps the audit found in the Mini App admin surface:
- banned (status=BLOCKED) users were NOT enforced anywhere in the Mini
  App API (the bot blocks them via AccessControlMiddleware);
- no user search / ban / unban endpoints for the Mini App;
- adjust-user mutated balances with no reason and no audit trail;
- queue cancel never refunded the job's charge;
- queue retry accepted ANY job status (even SUCCESS/RUNNING);
- overview lacked production health signals (failed 24h, banned,
  DB/storage size, recent admin actions).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.models.enums import AccountCategory, JobStatus, JobType, UserStatus
from app.models.schema import AuditLog, Job, MinuteBalance, MinuteLedgerEvent, User
from app.webapp import api as api_mod

ADMIN_ID = 999001
USER_ID = 700111
OTHER_ID = 700222


def _user(uid: int, **kw) -> User:
    return User(
        id=uid,
        first_name=kw.pop("first_name", "Test"),
        username=kw.pop("username", None),
        account_category=kw.pop("account_category", AccountCategory.FREE.value),
        language_code="my",
        **kw,
    )


@pytest.fixture(autouse=True)
def _admin_settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    yield


async def _seed(db, *objs):
    for o in objs:
        db.add(o)
    await db.commit()


# ── Ban / unban ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ban_requires_reason(db_session):
    admin, target = _user(ADMIN_ID), _user(USER_ID)
    await _seed(db_session, admin, target)
    with pytest.raises(HTTPException) as exc:
        await api_mod.ban_admin_user(USER_ID, api_mod.BanUserRequest(reason="x"), admin, db_session)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_ban_sets_status_and_writes_audit(db_session):
    admin, target = _user(ADMIN_ID), _user(USER_ID)
    await _seed(db_session, admin, target)
    res = await api_mod.ban_admin_user(
        USER_ID, api_mod.BanUserRequest(reason="spam abuse"), admin, db_session
    )
    assert res["success"] is True
    refreshed = await db_session.get(User, USER_ID)
    assert refreshed.status == UserStatus.BLOCKED.value
    logs = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "user.ban", AuditLog.target_id == str(USER_ID))
    )).scalars().all()
    assert len(logs) == 1
    assert json.loads(logs[0].details_json)["reason"] == "spam abuse"


@pytest.mark.asyncio
async def test_ban_admin_or_self_rejected(db_session):
    admin = _user(ADMIN_ID)
    other_admin = _user(ADMIN_ID + 1)
    monkeypatch_ids = str(ADMIN_ID) + "," + str(ADMIN_ID + 1)
    settings.admin_ids_raw = monkeypatch_ids
    await _seed(db_session, admin, other_admin)
    with pytest.raises(HTTPException):
        await api_mod.ban_admin_user(ADMIN_ID, api_mod.BanUserRequest(reason="test"), admin, db_session)
    with pytest.raises(HTTPException):
        await api_mod.ban_admin_user(ADMIN_ID + 1, api_mod.BanUserRequest(reason="test"), admin, db_session)


@pytest.mark.asyncio
async def test_unban_restores_active(db_session):
    admin = _user(ADMIN_ID)
    target = _user(USER_ID, status=UserStatus.BLOCKED.value)
    await _seed(db_session, admin, target)
    res = await api_mod.unban_admin_user(USER_ID, api_mod.BanUserRequest(reason="appeal ok"), admin, db_session)
    assert res["success"] is True
    refreshed = await db_session.get(User, USER_ID)
    assert refreshed.status == UserStatus.ACTIVE.value


# ── Ban enforcement in the Mini App gate ─────────────────────────────

@pytest.mark.asyncio
async def test_gate_rejects_banned_user(db_session):
    banned = _user(USER_ID, status=UserStatus.BLOCKED.value)
    await _seed(db_session, banned)
    with pytest.raises(HTTPException) as exc:
        await api_mod.require_gate_verified(banned, db_session)
    assert exc.value.status_code == 403
    assert "USER_BANNED" in str(exc.value.detail)


# ── User search ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_search_by_name_username_id(db_session):
    admin = _user(ADMIN_ID)
    a = _user(USER_ID, first_name="Aung Khant", username="aungkhant")
    b = _user(OTHER_ID, first_name="Su Su", username="susu")
    await _seed(db_session, admin, a, b)
    by_name = await api_mod.search_admin_users("aung", 20, admin, db_session)
    assert [u["id"] for u in by_name["users"]] == [USER_ID]
    by_username = await api_mod.search_admin_users("@susu", 20, admin, db_session)
    assert [u["id"] for u in by_username["users"]] == [OTHER_ID]
    by_id = await api_mod.search_admin_users(str(USER_ID), 20, admin, db_session)
    assert [u["id"] for u in by_id["users"]] == [USER_ID]
    with pytest.raises(HTTPException):
        await api_mod.search_admin_users("a", 20, admin, db_session)


# ── Adjust-user: reason + audited ledger ─────────────────────────────

@pytest.mark.asyncio
async def test_adjust_requires_reason(db_session):
    admin, target = _user(ADMIN_ID), _user(USER_ID)
    await _seed(db_session, admin, target)
    with pytest.raises(HTTPException) as exc:
        await api_mod.adjust_user_status(
            api_mod.AdjustUserRequest(user_id=USER_ID, add_minutes=30), admin, db_session
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_adjust_minutes_credits_and_audits(db_session):
    admin, target = _user(ADMIN_ID), _user(USER_ID)
    await _seed(db_session, admin, target)
    res = await api_mod.adjust_user_status(
        api_mod.AdjustUserRequest(
            user_id=USER_ID, add_minutes=30, add_recap_credits=5, reason="support goodwill"
        ),
        admin, db_session,
    )
    assert res["success"] is True
    bal = await db_session.get(MinuteBalance, USER_ID)
    assert bal is not None and bal.total_usable_seconds >= 30 * 60
    refreshed = await db_session.get(User, USER_ID)
    assert refreshed.recap_credits == 3 + 5  # schema default 3 + 5
    logs = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "user.adjust", AuditLog.target_id == str(USER_ID))
    )).scalars().all()
    assert logs and json.loads(logs[-1].details_json)["reason"] == "support goodwill"


@pytest.mark.asyncio
async def test_adjust_debit_beyond_balance_rejected(db_session):
    admin, target = _user(ADMIN_ID), _user(USER_ID)
    await _seed(db_session, admin, target)
    with pytest.raises(HTTPException) as exc:
        await api_mod.adjust_user_status(
            api_mod.AdjustUserRequest(user_id=USER_ID, add_minutes=-500, reason="clawback"),
            admin, db_session,
        )
    assert exc.value.status_code == 400


# ── Queue cancel: refund semantics ───────────────────────────────────

def _voice_job(jid: str, status: str) -> Job:
    return Job(
        id=jid, user_id=USER_ID, status=status,
        job_type=JobType.VOICE_CLONE.value, engine="voxcpm", input_text="hello",
    )


@pytest.mark.asyncio
async def test_cancel_voice_job_refunds_seconds(db_session):
    admin, target = _user(ADMIN_ID), _user(USER_ID)
    job = _voice_job("job-cancel-1", JobStatus.RUNNING.value)
    bal = MinuteBalance(user_id=USER_ID, purchased_seconds=0)
    debit = MinuteLedgerEvent(
        user_id=USER_ID, source="PURCHASE", direction="DEBIT",
        seconds=120, reference_id="job-cancel-1",
    )
    await _seed(db_session, admin, target)
    await _seed(db_session, job, bal, debit)
    res = await api_mod.cancel_queued_job("job-cancel-1", admin, db_session)
    assert res["success"] is True
    assert res["refund"]["refunded"] is True
    refreshed_job = await db_session.get(Job, "job-cancel-1")
    assert refreshed_job.status == JobStatus.CANCELLED.value
    refreshed_bal = await db_session.get(MinuteBalance, USER_ID)
    assert refreshed_bal.purchased_seconds == 120
    logs = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "job.cancel")
    )).scalars().all()
    assert len(logs) == 1
    # A second cancel must be refused (no double refund path).
    with pytest.raises(HTTPException):
        await api_mod.cancel_queued_job("job-cancel-1", admin, db_session)


@pytest.mark.asyncio
async def test_cancel_finished_job_rejected(db_session):
    admin, target = _user(ADMIN_ID), _user(USER_ID)
    job = _voice_job("job-done-1", JobStatus.SUCCESS.value)
    await _seed(db_session, admin, target)
    await _seed(db_session, job)
    with pytest.raises(HTTPException) as exc:
        await api_mod.cancel_queued_job("job-done-1", admin, db_session)
    assert exc.value.status_code == 400


# ── Queue retry guard ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_success_job_rejected(db_session):
    admin, target = _user(ADMIN_ID), _user(USER_ID)
    job = _voice_job("job-ok-1", JobStatus.SUCCESS.value)
    await _seed(db_session, admin, target)
    await _seed(db_session, job)
    with pytest.raises(HTTPException) as exc:
        await api_mod.retry_queued_job("job-ok-1", admin, db_session)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_retry_failed_voice_job_requeues(db_session):
    admin, target = _user(ADMIN_ID), _user(USER_ID)
    job = _voice_job("job-fail-1", JobStatus.FAILED.value)
    await _seed(db_session, admin, target)
    await _seed(db_session, job)
    res = await api_mod.retry_queued_job("job-fail-1", admin, db_session)
    assert res["success"] is True
    refreshed = await db_session.get(Job, "job-fail-1")
    assert refreshed.status == JobStatus.QUEUED.value


# ── Overview health + audit log ──────────────────────────────────────

@pytest.mark.asyncio
async def test_overview_includes_ops_health(db_session):
    admin = _user(ADMIN_ID)
    banned = _user(USER_ID, status=UserStatus.BLOCKED.value)
    failed = _voice_job("job-f24", JobStatus.FAILED.value)
    await _seed(db_session, admin, banned)
    await _seed(db_session, failed)
    data = await api_mod.get_admin_overview(admin, db_session)
    assert data["banned_users"] == 1
    assert data["failed_jobs_24h"] >= 1
    assert data["jobs_by_status"][JobStatus.FAILED.value] >= 1
    assert "recent_admin_actions" in data
    assert "db_size_bytes" in data and "storage_bytes" in data


@pytest.mark.asyncio
async def test_audit_log_endpoint_lists_actions(db_session):
    admin, target = _user(ADMIN_ID), _user(USER_ID)
    await _seed(db_session, admin, target)
    await api_mod.ban_admin_user(USER_ID, api_mod.BanUserRequest(reason="spam"), admin, db_session)
    rows = await api_mod.get_admin_audit_log(20, admin, db_session)
    assert any(r["action"] == "user.ban" for r in rows)


# ── Admin-only wiring (routes depend on require_admin) ───────────────

def test_new_admin_routes_require_admin_dependency():
    wanted = {
        "/api/admin/users/search",
        "/api/admin/users/{user_id}/ban",
        "/api/admin/users/{user_id}/unban",
        "/api/admin/audit-log",
    }
    found = {}
    for route in api_mod.api_router.routes:
        if route.path in wanted:
            deps = [getattr(d, "call", None) for d in route.dependant.dependencies]
            found[route.path] = api_mod.require_admin in deps
    assert found == {p: True for p in wanted}


# ── Frontend wiring (static) ─────────────────────────────────────────

def test_frontend_wiring_present():
    root = Path(__file__).resolve().parents[2]
    app_js = (root / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
    i18n_js = (root / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
    index_html = (root / "app/webapp/static/index.html").read_text(encoding="utf-8")
    assert "/api/admin/users/search" in app_js
    assert "adminSearchUsers" in app_js
    assert "_adminRequireReason" in app_js
    assert "is_banned" in app_js and "banned" in app_js
    assert "admin-ops-card" in index_html
    assert "adm-user-search-q" in index_html
    assert "adm-adjust-reason" in index_html
    # i18n parity for the new keys in both languages
    for key in ("gate_banned_title", "admin_reason_required", "btn_ban_user", "admin_recent_actions"):
        assert i18n_js.count(key + ":") >= 2, key
