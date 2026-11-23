"""Regression pins for the 2026-11-23 /api/user/me serialization & SQL hotfix."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.base import Base
from app.models.enums import AccountCategory
from app.models.schema import BotSetting, DailyFreeAllowanceGrant, User, WalletGrant, utc_now
from app.services.billing import create_wallet_grant, expire_due_grants_for_user, get_wallet_snapshot
from app.services.dynamic_settings import (
    get_dynamic_setting,
    get_dynamic_settings_batch,
    invalidate_dynamic_settings_cache,
    set_dynamic_setting,
)
from app.services.ledger import (
    get_current_daily_period,
    get_or_create_user,
    grant_daily_free_allowance,
)
from app.webapp import api as api_mod


@pytest.mark.asyncio
async def test_dynamic_settings_batch_parity_caching_and_invalidation(tmp_path):
    """Batched dynamic settings read matches single-key fallback, caches on persistent binds,
    and invalidates immediately on set_dynamic_setting() or direct ORM BotSetting flush."""
    db_file = tmp_path / "dyn_cache.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}", future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionMaker = async_sessionmaker(bind=eng, class_=AsyncSession, expire_on_commit=False, autoflush=False)

    sql_calls: list[str] = []

    @event.listens_for(eng.sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        if "bot_settings" in statement.lower():
            sql_calls.append(statement)

    orig_link = settings.tutorial_plans_link
    try:
        keys = ("tutorial_video_link", "tutorial_plans_link", "recap_demo_asset")
        defaults = {
            "tutorial_video_link": "https://t.me/VoiceClonePro/28",
            "tutorial_plans_link": "https://t.me/VoiceClonePro/31",
            "recap_demo_asset": "",
        }

        async with SessionMaker() as s:
            await set_dynamic_setting(s, "tutorial_plans_link", "https://t.me/VoiceClonePro/901", admin_id=1)
            await s.commit()

        invalidate_dynamic_settings_cache()
        sql_calls.clear()

        # 1. Cold miss: fetches all 3 keys in ONE SQL query
        async with SessionMaker() as s:
            batch1 = await get_dynamic_settings_batch(s, keys, defaults=defaults)
            assert len(sql_calls) == 1
            for k in keys:
                assert batch1[k] == await get_dynamic_setting(s, k, default=defaults[k])
            assert batch1["tutorial_plans_link"] == "https://t.me/VoiceClonePro/901"

        # 2. Warm hit: 0 SQL queries
        sql_calls.clear()
        async with SessionMaker() as s:
            batch2 = await get_dynamic_settings_batch(s, keys, defaults=defaults)
            assert len(sql_calls) == 0
            assert batch2 == batch1

        # 3. Admin write via set_dynamic_setting invalidates cache immediately
        async with SessionMaker() as s:
            await set_dynamic_setting(s, "tutorial_plans_link", "https://t.me/VoiceClonePro/902", admin_id=1)
            await s.commit()
        sql_calls.clear()
        async with SessionMaker() as s:
            batch3 = await get_dynamic_settings_batch(s, keys, defaults=defaults)
            assert len(sql_calls) == 1
            assert batch3["tutorial_plans_link"] == "https://t.me/VoiceClonePro/902"

        # 4. Direct ORM BotSetting write also invalidates via after_flush listener
        async with SessionMaker() as s:
            row = (await s.execute(select(BotSetting).where(BotSetting.key == "tutorial_plans_link"))).scalar_one()
            row.value = "https://t.me/VoiceClonePro/903"
            await s.commit()
        sql_calls.clear()
        async with SessionMaker() as s:
            batch4 = await get_dynamic_settings_batch(s, keys, defaults=defaults)
            assert len(sql_calls) == 1
            assert batch4["tutorial_plans_link"] == "https://t.me/VoiceClonePro/903"
    finally:
        settings.tutorial_plans_link = orig_link
        invalidate_dynamic_settings_cache()
        await eng.dispose()


@pytest.mark.asyncio
async def test_daily_allowance_repeat_call_and_admin_take_zero_for_update_locks(tmp_path):
    """Repeat same-day FREE call, PURCHASED call, and admin call take zero FOR UPDATE locks
    and perform zero writes, while first-of-day FREE call still grants idempotently."""
    db_file = tmp_path / "daily_lock.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}", future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionMaker = async_sessionmaker(bind=eng, class_=AsyncSession, expire_on_commit=False, autoflush=False)

    for_update_stmts: list[str] = []
    write_stmts: list[str] = []

    @event.listens_for(eng.sync_engine, "before_cursor_execute")
    def _spy(conn, cursor, statement, parameters, context, executemany):
        compiled = getattr(getattr(context, "compiled", None), "statement", None)
        if getattr(compiled, "_for_update_arg", None) is not None or "FOR UPDATE" in statement.upper():
            for_update_stmts.append(statement)
        up = statement.strip().upper()
        if up.startswith(("INSERT", "UPDATE", "DELETE")):
            write_stmts.append(statement)

    orig_admins = settings.admin_ids_raw
    try:
        settings.admin_ids_raw = "777001"
        async with SessionMaker() as s:
            free_u, _ = await get_or_create_user(s, telegram_id=777002, username="f", first_name="F")
            paid_u, _ = await get_or_create_user(s, telegram_id=777003, username="p", first_name="P")
            paid_u.account_category = AccountCategory.PURCHASED.value
            admin_u, _ = await get_or_create_user(s, telegram_id=777001, username="a", first_name="A")
            await s.commit()

        # First-of-day FREE grant: acquires FOR UPDATE and writes 1 grant
        for_update_stmts.clear()
        write_stmts.clear()
        async with SessionMaker() as s:
            granted, bal = await grant_daily_free_allowance(s, free_u.id)
            assert granted is True
            assert bal.daily_free_seconds == 180
            assert bal.last_daily_grant_date == get_current_daily_period()
            await s.commit()
        assert len(for_update_stmts) == 1
        assert len(write_stmts) >= 2

        # Repeat same-day FREE call, PURCHASED call, and admin call: 0 FOR UPDATE, 0 writes
        for uid in (free_u.id, paid_u.id, admin_u.id):
            for_update_stmts.clear()
            write_stmts.clear()
            async with SessionMaker() as s:
                granted, _ = await grant_daily_free_allowance(s, uid)
                assert granted is False
            assert for_update_stmts == [], f"Unexpected FOR UPDATE for uid={uid}"
            assert write_stmts == [], f"Unexpected write for uid={uid}"
    finally:
        settings.admin_ids_raw = orig_admins
        await eng.dispose()


@pytest.mark.asyncio
async def test_wallet_snapshot_lock_free_when_not_overdue_and_expires_when_overdue(tmp_path):
    """get_wallet_snapshot & expire_due_grants_for_user skip FOR UPDATE when no grant is due,
    and still expire + downgrade accurately when a grant IS overdue."""
    db_file = tmp_path / "wallet_lock.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}", future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionMaker = async_sessionmaker(bind=eng, class_=AsyncSession, expire_on_commit=False, autoflush=False)

    for_update_stmts: list[str] = []

    @event.listens_for(eng.sync_engine, "before_cursor_execute")
    def _spy(conn, cursor, statement, parameters, context, executemany):
        compiled = getattr(getattr(context, "compiled", None), "statement", None)
        if getattr(compiled, "_for_update_arg", None) is not None or "FOR UPDATE" in statement.upper():
            for_update_stmts.append(statement)

    try:
        async with SessionMaker() as s:
            u, _ = await get_or_create_user(s, telegram_id=777010, username="w", first_name="W")
            u.account_category = AccountCategory.PURCHASED.value
            await create_wallet_grant(
                s, u.id, voice_seconds=600, recap_seconds=120, validity_days=30,
                package_code="pkg_starter", package_name="Starter",
            )
            await s.commit()

        # Active future grant -> 0 FOR UPDATE locks
        for_update_stmts.clear()
        async with SessionMaker() as s:
            snap = await get_wallet_snapshot(s, u.id)
            assert snap["account_category"] == "PURCHASED"
            assert snap["active_grants_count"] == 1
        assert for_update_stmts == []

        # Push grant into the past -> expire_due_grants_for_user runs & downgrades to FREE
        async with SessionMaker() as s:
            g = (await s.execute(select(WalletGrant).where(WalletGrant.user_id == u.id))).scalar_one()
            g.expires_at = utc_now() - timedelta(hours=1)
            await s.commit()

        for_update_stmts.clear()
        async with SessionMaker() as s:
            snap2 = await get_wallet_snapshot(s, u.id)
            await s.commit()
            assert snap2["account_category"] == "FREE"
            assert snap2["active_grants_count"] == 0
        assert len(for_update_stmts) >= 1
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_user_me_sql_budget_zero_locks_and_concurrent_safety(tmp_path):
    """Warm /api/user/me executes <= 9 SQL statements, zero FOR UPDATE locks, zero commits,
    and handles 10 concurrent calls cleanly with identical payloads."""
    db_file = tmp_path / "me_perf.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}", future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionMaker = async_sessionmaker(bind=eng, class_=AsyncSession, expire_on_commit=False, autoflush=False)

    sql_events: list[str] = []
    for_update_hits: list[str] = []
    recording = False

    @event.listens_for(eng.sync_engine, "before_cursor_execute")
    def _spy(conn, cursor, statement, parameters, context, executemany):
        if not recording:
            return
        sql_events.append(statement)
        compiled = getattr(getattr(context, "compiled", None), "statement", None)
        if getattr(compiled, "_for_update_arg", None) is not None or "FOR UPDATE" in statement.upper():
            for_update_hits.append(statement)

    try:
        async with SessionMaker() as s:
            u, _ = await get_or_create_user(s, telegram_id=777020, username="meperf", first_name="MePerf")
            await grant_daily_free_allowance(s, u.id)
            await s.commit()

        # Prime caches & thumbnail table check once
        async with SessionMaker() as s:
            u = await s.get(User, 777020)
            p_warm = await api_mod.get_user_profile(u, s)

        # Measure single warm /api/user/me call
        sql_events.clear()
        for_update_hits.clear()
        recording = True
        async with SessionMaker() as s:
            u = await s.get(User, 777020)
            p1 = await api_mod.get_user_profile(u, s)
        recording = False

        # Includes the 1 SELECT users from s.get(User, ...)
        assert len(sql_events) <= 9, f"Expected <= 9 SQL statements, got {len(sql_events)}: {sql_events}"
        assert for_update_hits == [], f"Expected 0 FOR UPDATE locks, got {for_update_hits}"
        assert p1 == p_warm

        # 10 concurrent calls return identical payloads without error
        async def _call_once():
            async with SessionMaker() as s:
                u_obj = await s.get(User, 777020)
                return await api_mod.get_user_profile(u_obj, s)

        results = await asyncio.gather(*[_call_once() for _ in range(10)])
        assert len(results) == 10
        for r in results:
            assert r == p1
    finally:
        invalidate_dynamic_settings_cache()
        await eng.dispose()


def test_load_user_profile_has_no_dangling_paint_plan_how_text_call():
    """Guard against ReferenceError inside loadUserProfile() after Part II deleted paintPlanHowText."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
    assert "paintPlanHowText(" not in js
