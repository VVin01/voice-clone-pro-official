"""Admin rebuild (branch feat-admin-rebuild-2026-10-31).

Covers the endpoints ADDED by the rebuild's functional audit — the ones a
complete Mini App admin needs but did not have:

- GET  /admin/users            recent-users browse list (search needs a query)
- GET  /admin/users/{user_id}  user detail with job statistics
- GET  /admin/history          user_id + job_id-prefix narrowing filters
- GET  /admin/pricing          plan price list (was bot-only until now)
- POST /admin/pricing/{code}   audited price edit, same bounds as the bot

All endpoints stay behind require_admin (checked via route dependencies),
and the pre-existing semantics they sit next to are untouched.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.models.enums import AccountCategory, JobStatus, JobType, UserStatus
from app.models.schema import AuditLog, Job, MinuteBalance, PricingPlan, User
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


def _job(jid: str, uid: int, status: str, **kw) -> Job:
    return Job(
        id=jid,
        user_id=uid,
        status=status,
        job_type=kw.pop("job_type", JobType.VOICE_CLONE.value),
        input_text=kw.pop("input_text", "hello world"),
        engine=kw.pop("engine", "voxcpm"),
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


# ── Recent users list ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_users_returns_recent_first_with_balances(db_session):
    admin = _user(ADMIN_ID, created_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
    older = _user(USER_ID, username="older", created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    newer = _user(OTHER_ID, username="newer", created_at=datetime(2026, 10, 30, tzinfo=timezone.utc))
    await _seed(db_session, admin, older, newer)
    await _seed(db_session, MinuteBalance(user_id=OTHER_ID, purchased_seconds=3660))
    res = await api_mod.list_admin_users(30, 0, admin, db_session)
    ids = [u["id"] for u in res["users"]]
    assert ids[0] == OTHER_ID  # newest first
    row = next(u for u in res["users"] if u["id"] == OTHER_ID)
    assert row["balance_seconds"] == 3660
    assert row["is_banned"] is False
    assert res["count"] == len(res["users"])


@pytest.mark.asyncio
async def test_list_users_respects_limit_and_offset(db_session):
    admin = _user(ADMIN_ID)
    users = [admin] + [
        _user(700300 + i, created_at=datetime(2026, 10, i + 1, tzinfo=timezone.utc))
        for i in range(5)
    ]
    await _seed(db_session, *users)
    page1 = await api_mod.list_admin_users(2, 0, admin, db_session)
    page2 = await api_mod.list_admin_users(2, 2, admin, db_session)
    assert page1["count"] == 2 and page2["count"] == 2
    assert not {u["id"] for u in page1["users"]} & {u["id"] for u in page2["users"]}
    clamped = await api_mod.list_admin_users(9999, 0, admin, db_session)
    assert clamped["limit"] == 100


# ── User detail ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_detail_includes_job_stats(db_session):
    admin = _user(ADMIN_ID)
    target = _user(USER_ID, username="aung", recap_credits=7,
                   status=UserStatus.BLOCKED.value)
    jobs = [
        _job("job-ok-1", USER_ID, JobStatus.SUCCESS.value),
        _job("job-ok-2", USER_ID, JobStatus.SUCCESS.value),
        _job("job-bad-1", USER_ID, JobStatus.FAILED.value),
        _job("job-live-1", USER_ID, JobStatus.RUNNING.value),
        _job("job-other", OTHER_ID, JobStatus.SUCCESS.value),
    ]
    await _seed(db_session, admin, target, _user(OTHER_ID))
    await _seed(db_session, *jobs,
                MinuteBalance(user_id=USER_ID, purchased_seconds=600))
    res = await api_mod.get_admin_user_detail(USER_ID, admin, db_session)
    assert res["username"] == "aung"
    assert res["is_banned"] is True
    assert res["recap_credits"] == 7
    assert res["balance_seconds"] == 600
    assert res["jobs_total"] == 4
    assert res["jobs_success"] == 2
    assert res["jobs_failed"] == 1
    assert res["jobs_active"] == 1
    assert res["last_job_at"] is not None


@pytest.mark.asyncio
async def test_user_detail_missing_user_404(db_session):
    admin = _user(ADMIN_ID)
    await _seed(db_session, admin)
    with pytest.raises(HTTPException) as exc:
        await api_mod.get_admin_user_detail(424242, admin, db_session)
    assert exc.value.status_code == 404


# ── History narrowing filters ────────────────────────────────────────

async def _history(db, **kw):
    admin = await db.get(User, ADMIN_ID)
    return await api_mod.get_admin_global_history(user=admin, db=db, **kw)


@pytest.mark.asyncio
async def test_history_user_id_filter(db_session):
    await _seed(db_session, _user(ADMIN_ID), _user(USER_ID), _user(OTHER_ID))
    await _seed(
        db_session,
        _job("aaaa1111", USER_ID, JobStatus.SUCCESS.value),
        _job("bbbb2222", OTHER_ID, JobStatus.SUCCESS.value),
    )
    res = await _history(db_session, timeframe="all", user_id=USER_ID)
    assert res["total_count"] == 1
    assert res["history"][0]["id"] == "aaaa1111"
    assert res["user_id_filter"] == USER_ID


@pytest.mark.asyncio
async def test_history_job_id_prefix_search(db_session):
    await _seed(db_session, _user(ADMIN_ID), _user(USER_ID))
    await _seed(
        db_session,
        _job("rc_88f2c41d", USER_ID, JobStatus.FAILED.value,
             job_type=JobType.RECAP.value, engine="recap"),
        _job("vc_12345678", USER_ID, JobStatus.SUCCESS.value),
    )
    res = await _history(db_session, timeframe="all", job_id="rc_88f2")
    assert res["total_count"] == 1
    assert res["history"][0]["id"] == "rc_88f2c41d"
    res_all = await _history(db_session, timeframe="all")
    assert res_all["total_count"] == 2  # unfiltered callers unaffected


# ── Pricing ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pricing_list_returns_active_plans(db_session):
    admin = _user(ADMIN_ID)
    await _seed(
        db_session, admin,
        PricingPlan(code="MIN_10", minutes=10, price_mmk=8000, is_active=True, sort_order=1),
        PricingPlan(code="MIN_30", minutes=30, price_mmk=20000, is_active=True, sort_order=2),
    )
    res = await api_mod.get_admin_pricing(admin, db_session)
    codes = [p["code"] for p in res["plans"]]
    assert codes == ["MIN_10", "MIN_30"]
    assert res["plans"][0]["price_mmk"] == 8000
    assert res["plans"][0]["category"] == "voice"


@pytest.mark.asyncio
async def test_pricing_update_writes_audit(db_session):
    admin = _user(ADMIN_ID)
    await _seed(
        db_session, admin,
        PricingPlan(code="MIN_10", minutes=10, price_mmk=8000, is_active=True, sort_order=1),
    )
    res = await api_mod.update_admin_pricing(
        "MIN_10", api_mod.AdminPricingUpdateRequest(price_mmk=9500), admin, db_session
    )
    assert res["success"] is True and res["price_mmk"] == 9500
    plan = (await db_session.execute(
        select(PricingPlan).where(PricingPlan.code == "MIN_10")
    )).scalar_one()
    assert plan.price_mmk == 9500
    logs = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "pricing.update")
    )).scalars().all()
    assert len(logs) == 1
    details = json.loads(logs[0].details_json)
    assert details == {"old_price": 8000, "new_price": 9500}


@pytest.mark.asyncio
async def test_pricing_update_bounds_and_unknown_plan(db_session):
    admin = _user(ADMIN_ID)
    await _seed(
        db_session, admin,
        PricingPlan(code="MIN_10", minutes=10, price_mmk=8000, is_active=True, sort_order=1),
    )
    with pytest.raises(HTTPException) as exc:
        await api_mod.update_admin_pricing(
            "MIN_10", api_mod.AdminPricingUpdateRequest(price_mmk=50), admin, db_session
        )
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        await api_mod.update_admin_pricing(
            "MIN_10", api_mod.AdminPricingUpdateRequest(price_mmk=10_000_001), admin, db_session
        )
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        await api_mod.update_admin_pricing(
            "NOPE", api_mod.AdminPricingUpdateRequest(price_mmk=5000), admin, db_session
        )
    assert exc.value.status_code == 404


# ── Admin-only wiring for every new route ────────────────────────────

def test_rebuild_routes_require_admin_dependency():
    wanted = {
        "/api/admin/users",
        "/api/admin/users/{user_id}",
        "/api/admin/pricing",
        "/api/admin/pricing/{code}",
    }
    found = {}
    for route in api_mod.api_router.routes:
        if route.path in wanted:
            deps = [getattr(d, "call", None) for d in route.dependant.dependencies]
            found[route.path] = api_mod.require_admin in deps
    assert found == {p: True for p in wanted}
