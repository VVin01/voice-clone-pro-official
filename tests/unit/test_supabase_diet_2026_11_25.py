"""Unit tests for Part A — Supabase Egress Diet Design 2 (2026-11-25).

Pins:
1. Watermark + strict allowlist (`CONFIG_SETTING_KEYS`, `refresh_dynamic_settings_if_changed`,
   `load_dynamic_settings`):
   - Exact 78-key allowlist (`DYNAMIC_SETTING_KEYS - HEARTBEAT_SETTING_KEYS`).
   - Unchanged watermark tuple -> zero full reloads across repeated refreshes.
   - Changed config row -> exactly one allowlisted reload.
   - State blobs (`bn_key_health_v1`, `aj_hf:*`, `aj_kh:*`, `aj_gai:*`,
     `user_history_cleared_at:*`) and the 3 heartbeat keys (`worker_laptop_*`)
     are never returned by `load_dynamic_settings()` and never advance the
     watermark probe even when newer.
2. `get_dynamic_setting()` routes through `_DYNAMIC_SETTINGS_CACHE` (0 extra SQL
   within 15 s TTL) and observes `set_dynamic_setting()` writes immediately in-process.
3. `WorkerPollBackoff`: 2 -> 5 -> 10 idle sequence, 20 s warm window after a completed
   job, reset on claim, 5 -> 15 -> 30 DB-error backoff, and single-session tick
   short-circuit when settings refresh fails.
4. Probe/claim SQL filter parity across all `(engine_filter, allow_recap, skip_local_source)`
   combinations, including the `recap_ref_audio_path` portability clause.
5. Secondary worker heartbeat: single `INSERT ... ON CONFLICT DO UPDATE` statement
   (no preceding SELECT), `engine`/`host` written on first tick and on change only,
   and `_get_laptop_worker_status` batched query + 150/300 thresholds (149/150/151
   and 299/300/301 boundaries).
6. `key_rotation._sync_engine` pool arguments `(1, 1, 1800)` and `tts_leak_gate`
   `_MISS_SENTINEL` caching (two consecutive unset-mode reads -> one DB read).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import worker as worker_mod
from app.config import settings
from app.models.enums import JobStatus, JobType
from app.models.schema import Base, BotSetting, Job, User
from app.services import dynamic_settings as ds_mod
from app.services import key_rotation as kr_mod
from app.services import queue as queue_mod
from app.services import tts_leak_gate as leak_mod
from app.webapp import api as api_mod


async def _open_file_db(tmp_path: Path, name: str = "diet.db"):
    db_file = tmp_path / name
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}", future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(
        bind=eng,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    return eng, maker


def test_config_setting_keys_strict_allowlist_has_78_keys():
    expected = ds_mod.DYNAMIC_SETTING_KEYS - {
        "worker_laptop_heartbeat",
        "worker_laptop_engine",
        "worker_laptop_host",
    }
    assert ds_mod.CONFIG_SETTING_KEYS == expected
    assert len(ds_mod.CONFIG_SETTING_KEYS) == len(ds_mod.DYNAMIC_SETTING_KEYS) - 3
    for forbidden in (
        "bn_key_health_v1",
        "aj_hf:pool_state",
        "aj_kh:gemini",
        "aj_gai:limiter:v1",
        "user_history_cleared_at:123",
        "worker_laptop_heartbeat",
        "worker_laptop_engine",
        "worker_laptop_host",
    ):
        assert forbidden not in ds_mod.CONFIG_SETTING_KEYS


@pytest.mark.asyncio
async def test_watermark_refresh_and_allowlist_exclude_blobs_and_heartbeats(tmp_path, monkeypatch):
    eng, maker = await _open_file_db(tmp_path, "watermark.db")
    sql_stmts: list[str] = []

    @event.listens_for(eng.sync_engine, "before_cursor_execute")
    def _spy(conn, cursor, statement, parameters, context, executemany):
        sql_stmts.append(statement)

    clock = {"t": 1000.0}
    monkeypatch.setattr(ds_mod.time, "monotonic", lambda: clock["t"])
    ds_mod.reset_dynamic_settings_watermark()
    ds_mod.invalidate_dynamic_settings_cache()

    orig_note = settings.maintenance_note
    try:
        t0 = datetime(2026, 11, 25, 4, 0, 0, tzinfo=timezone.utc)
        t_newer = t0 + timedelta(minutes=10)
        async with maker() as s:
            s.add_all(
                [
                    BotSetting(key="maintenance_note", value="initial-note", updated_at=t0),
                    BotSetting(key="bn_key_health_v1", value="X" * 35000, updated_at=t_newer),
                    BotSetting(key="aj_hf:pool_state", value="Y" * 15000, updated_at=t_newer),
                    BotSetting(key="aj_kh:gemini", value="{}", updated_at=t_newer),
                    BotSetting(key="aj_gai:limiter:v1", value="{}", updated_at=t_newer),
                    BotSetting(key="user_history_cleared_at:777", value="2026-11-25", updated_at=t_newer),
                    BotSetting(key="worker_laptop_heartbeat", value=t_newer.isoformat(), updated_at=t_newer),
                    BotSetting(key="worker_laptop_engine", value="voxcpm", updated_at=t_newer),
                    BotSetting(key="worker_laptop_host", value="vps-worker", updated_at=t_newer),
                ]
            )
            await s.commit()

        # Verify load_dynamic_settings directly excludes every non-allowlist row
        async with maker() as s:
            loaded = await ds_mod.load_dynamic_settings(s, quiet=True)
        assert loaded == {"maintenance_note": "initial-note"}
        assert settings.maintenance_note == "initial-note"

        # 1. First refresh on process/bind always loads once
        sql_stmts.clear()
        async with maker() as s:
            reloaded_1 = await ds_mod.refresh_dynamic_settings_if_changed(s, min_interval_s=15.0)
        assert reloaded_1 is True
        assert sum(1 for q in sql_stmts if "SELECT bot_settings." in q) == 1

        # 2. Within 15 s min_interval_s -> zero SQL
        clock["t"] += 5.0
        sql_stmts.clear()
        async with maker() as s:
            reloaded_gate = await ds_mod.refresh_dynamic_settings_if_changed(s, min_interval_s=15.0)
        assert reloaded_gate is False
        assert sql_stmts == []

        # 3. Repeated refreshes past 15 s with unchanged config -> watermark probe only, 0 full loads
        for _ in range(3):
            clock["t"] += 16.0
            sql_stmts.clear()
            async with maker() as s:
                reloaded = await ds_mod.refresh_dynamic_settings_if_changed(s, min_interval_s=15.0)
            assert reloaded is False
            assert len(sql_stmts) == 1
            assert "max(" in sql_stmts[0].lower() and "count(" in sql_stmts[0].lower()
            assert "SELECT bot_settings.key" not in sql_stmts[0]

        # 4. Updating blobs + heartbeat rows to even newer timestamps MUST NOT move watermark or reload
        t_latest_blob = t_newer + timedelta(minutes=20)
        async with maker() as s:
            for k in (
                "bn_key_health_v1",
                "aj_hf:pool_state",
                "aj_kh:gemini",
                "aj_gai:limiter:v1",
                "user_history_cleared_at:777",
                "worker_laptop_heartbeat",
                "worker_laptop_engine",
                "worker_laptop_host",
            ):
                row = await s.get(BotSetting, k)
                row.value = f"updated-{t_latest_blob.isoformat()}"
                row.updated_at = t_latest_blob
            await s.commit()

        clock["t"] += 16.0
        sql_stmts.clear()
        async with maker() as s:
            reloaded_after_blobs = await ds_mod.refresh_dynamic_settings_if_changed(s, min_interval_s=15.0)
        assert reloaded_after_blobs is False
        assert len(sql_stmts) == 1

        # 5. Updating a real config row moves watermark -> triggers EXACTLY ONE allowlisted reload
        t_cfg_bump = t_latest_blob + timedelta(seconds=5)
        async with maker() as s:
            cfg_row = await s.get(BotSetting, "maintenance_note")
            cfg_row.value = "updated-from-admin"
            cfg_row.updated_at = t_cfg_bump
            await s.commit()

        clock["t"] += 16.0
        sql_stmts.clear()
        async with maker() as s:
            reloaded_cfg = await ds_mod.refresh_dynamic_settings_if_changed(s, min_interval_s=15.0)
        assert reloaded_cfg is True
        assert settings.maintenance_note == "updated-from-admin"
        assert sum(1 for q in sql_stmts if "SELECT bot_settings." in q) == 1

        # Subsequent refresh returns to 0 full loads
        clock["t"] += 16.0
        sql_stmts.clear()
        async with maker() as s:
            assert await ds_mod.refresh_dynamic_settings_if_changed(s, min_interval_s=15.0) is False
        assert len(sql_stmts) == 1
    finally:
        settings.maintenance_note = orig_note
        ds_mod.reset_dynamic_settings_watermark()
        ds_mod.invalidate_dynamic_settings_cache()
        await eng.dispose()


@pytest.mark.asyncio
async def test_get_dynamic_setting_serves_from_cache_and_observes_set_immediately(tmp_path):
    eng, maker = await _open_file_db(tmp_path, "single_cache.db")
    select_stmts: list[str] = []

    @event.listens_for(eng.sync_engine, "before_cursor_execute")
    def _spy(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            select_stmts.append(statement)

    orig_val = settings.tutorial_video_link
    try:
        async with maker() as s:
            await ds_mod.set_dynamic_setting(s, "tutorial_video_link", "https://t.me/vcp/101", admin_id=1)
            await s.commit()

        ds_mod.invalidate_dynamic_settings_cache()
        select_stmts.clear()

        async with maker() as s:
            # Prime batch cache
            batch = await ds_mod.get_dynamic_settings_batch(
                s, ("tutorial_video_link", "tutorial_plans_link")
            )
            assert batch["tutorial_video_link"] == "https://t.me/vcp/101"
            assert len(select_stmts) == 1

            # Subsequent get_dynamic_setting within TTL hits cache (0 extra SELECTs)
            select_stmts.clear()
            val1 = await ds_mod.get_dynamic_setting(s, "tutorial_video_link")
            val2 = await ds_mod.get_dynamic_setting(s, "tutorial_plans_link", default="fallback")
            assert val1 == "https://t.me/vcp/101"
            assert val2 is not None
            assert select_stmts == []

            # set_dynamic_setting invalidates key immediately in-process
            await ds_mod.set_dynamic_setting(s, "tutorial_video_link", "https://t.me/vcp/202", admin_id=1)
            await s.commit()

            select_stmts.clear()
            updated = await ds_mod.get_dynamic_setting(s, "tutorial_video_link")
            assert updated == "https://t.me/vcp/202"
            assert len(select_stmts) == 1
    finally:
        settings.tutorial_video_link = orig_val
        ds_mod.invalidate_dynamic_settings_cache()
        await eng.dispose()


def test_worker_poll_backoff_idle_warm_window_claim_reset_and_db_errors():
    bo = worker_mod.WorkerPollBackoff()
    assert bo.idle_steps == (2.0, 5.0, 10.0)
    assert bo.warm_window_s == 20.0
    assert bo.error_steps == (5.0, 15.0, 30.0)

    # Cold idle sequence: 2 -> 5 -> 10 -> 10
    assert bo.on_empty_tick(100.0) == 2.0
    assert bo.on_empty_tick(102.0) == 5.0
    assert bo.on_empty_tick(107.0) == 10.0
    assert bo.on_empty_tick(117.0) == 10.0

    # Claim resets streak immediately
    bo.on_job_claimed()
    assert bo.empty_streak == 0

    # Completed job opens a 20 s warm window at 2.0 s, then steps 2 -> 5 -> 10
    bo.on_job_completed(200.0)
    assert bo.on_empty_tick(205.0) == 2.0
    assert bo.on_empty_tick(219.9) == 2.0
    # Warm window expired at 220.0 -> steps 2 -> 5 -> 10
    assert bo.on_empty_tick(220.0) == 2.0
    assert bo.on_empty_tick(222.0) == 5.0
    assert bo.on_empty_tick(227.0) == 10.0
    assert bo.on_empty_tick(237.0) == 10.0

    # DB-error sequence: 5 -> 15 -> 30 -> 30 (cap), reset on clean tick
    assert bo.on_db_error() == 5.0
    assert bo.on_db_error() == 15.0
    assert bo.on_db_error() == 30.0
    assert bo.on_db_error() == 30.0
    assert bo.on_empty_tick(300.0) == 10.0
    assert bo.on_db_error() == 5.0


@pytest.mark.asyncio
async def test_worker_tick_short_circuits_on_settings_refresh_failure():
    """A tick that fails in refresh_dynamic_settings_if_changed must not attempt probe or claim."""
    fake_session = AsyncMock()

    class _SessCtx:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    probe_mock = AsyncMock(return_value=True)
    claim_mock = AsyncMock(return_value=None)
    with (
        patch("app.worker.AsyncSessionLocal", return_value=_SessCtx()),
        patch(
            "app.services.dynamic_settings.refresh_dynamic_settings_if_changed",
            new=AsyncMock(side_effect=RuntimeError("db connection lost")),
        ),
        patch("app.services.queue.has_queued_job_probe", new=probe_mock),
        patch("app.services.queue.claim_next_queued_job", new=claim_mock),
    ):
        with pytest.raises(RuntimeError, match="db connection lost"):
            await worker_mod.claim_and_process_next_job(engine_filter="voxcpm")

    probe_mock.assert_not_awaited()
    claim_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_and_claim_sql_filter_parity_including_recap_ref_portability(db_session):
    from sqlalchemy.dialects import postgresql

    for eng_f in (None, "voxcpm", "edge"):
        for allow_rc in (False, True):
            for skip_local in (False, True):
                preds = queue_mod._queued_job_eligibility_predicates(
                    engine_filter=eng_f,
                    skip_local_source=skip_local,
                    allow_recap=allow_rc,
                )
                probe_sql = str(
                    select(1).select_from(Job).where(*preds).compile(
                        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                    )
                )
                claim_sql = str(
                    select(Job).where(*preds).compile(
                        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                    )
                )
                probe_where = probe_sql.split("WHERE ", 1)[1]
                claim_where = claim_sql.split("WHERE ", 1)[1]
                assert probe_where == claim_where
                if skip_local:
                    assert "recap_ref_audio_path IS NULL" in probe_where
                    assert "recap_ref_audio_path = ''" in probe_where

    # Behavioral check on seeded non-portable recap job (URL video + local ref audio)
    u = User(id=501001, username="probe_u", account_category="FREE")
    db_session.add(u)
    await db_session.flush()
    db_session.add(
        Job(
            id="rc_nonportable_ref",
            user_id=u.id,
            status=JobStatus.QUEUED.value,
            job_type=JobType.RECAP.value,
            engine="recap",
            source_audio_path="https://example.com/video.mp4",
            recap_ref_audio_path="/app/storage/temp/custom_ref.wav",
        )
    )
    db_session.add(
        Job(
            id="tts_never_worker",
            user_id=u.id,
            status=JobStatus.QUEUED.value,
            job_type=JobType.TTS.value,
            engine="voxcpm",
        )
    )
    await db_session.commit()

    # Clean-disk worker (skip_local_source=True) sees FALSE on the probe and claims None
    assert await queue_mod.has_queued_job_probe(
        db_session, engine_filter="voxcpm", skip_local_source=True, allow_recap=True
    ) is False
    assert await queue_mod.claim_next_queued_job(
        db_session, engine_filter="voxcpm", skip_local_source=True, allow_recap=True
    ) is None

    # Local-disk primary worker (skip_local_source=False, allow_recap=True) sees TRUE
    assert await queue_mod.has_queued_job_probe(
        db_session, engine_filter="voxcpm", skip_local_source=False, allow_recap=True
    ) is True


@pytest.mark.asyncio
async def test_heartbeat_single_upsert_change_detection_and_status_thresholds(tmp_path):
    eng, maker = await _open_file_db(tmp_path, "heartbeat.db")
    executed_sql: list[str] = []

    @event.listens_for(eng.sync_engine, "before_cursor_execute")
    def _spy(conn, cursor, statement, parameters, context, executemany):
        executed_sql.append(statement.strip())

    worker_mod.reset_worker_heartbeat_cache()
    ds_mod.invalidate_dynamic_settings_cache()
    try:
        assert worker_mod.WORKER_HEARTBEAT_INTERVAL_SECONDS == 60.0
        base_now = datetime(2026, 11, 25, 5, 0, 0, tzinfo=timezone.utc)

        # Tick 1: writes heartbeat + engine + host via single-statement upserts (no SELECTs)
        executed_sql.clear()
        async with maker() as s:
            keys_1 = await worker_mod.record_secondary_worker_heartbeat(
                s, engine_filter="voxcpm", host_override="node-a", now_dt=base_now
            )
        assert keys_1 == ["worker_laptop_heartbeat", "worker_laptop_engine", "worker_laptop_host"]
        select_q = [q for q in executed_sql if q.upper().startswith("SELECT")]
        upsert_q = [q for q in executed_sql if "ON CONFLICT" in q.upper()]
        assert select_q == []
        assert len(upsert_q) == 3

        # Tick 2 (unchanged engine & host): ONLY worker_laptop_heartbeat is written (1 upsert)
        executed_sql.clear()
        async with maker() as s:
            keys_2 = await worker_mod.record_secondary_worker_heartbeat(
                s,
                engine_filter="voxcpm",
                host_override="node-a",
                now_dt=base_now + timedelta(seconds=60),
            )
        assert keys_2 == ["worker_laptop_heartbeat"]
        assert [q for q in executed_sql if q.upper().startswith("SELECT")] == []
        assert len([q for q in executed_sql if "ON CONFLICT" in q.upper()]) == 1

        # Tick 3 (changed host): writes heartbeat + host (2 upserts)
        executed_sql.clear()
        hb_ts = base_now + timedelta(seconds=120)
        async with maker() as s:
            keys_3 = await worker_mod.record_secondary_worker_heartbeat(
                s,
                engine_filter="voxcpm",
                host_override="node-b",
                now_dt=hb_ts,
            )
        assert keys_3 == ["worker_laptop_heartbeat", "worker_laptop_host"]
        assert len([q for q in executed_sql if "ON CONFLICT" in q.upper()]) == 2

        # _get_laptop_worker_status: 1 batched SELECT on cold cache + exact 150/300 boundaries
        ds_mod.invalidate_dynamic_settings_cache()
        executed_sql.clear()
        async with maker() as s:
            st_149 = await api_mod._get_laptop_worker_status(
                s, now_utc=hb_ts + timedelta(seconds=149)
            )
        selects_cold = [q for q in executed_sql if q.upper().startswith("SELECT")]
        assert len(selects_cold) == 1
        assert st_149["status_code"] == "ONLINE"
        assert st_149["host"] == "node-b"

        async with maker() as s:
            assert (await api_mod._get_laptop_worker_status(s, now_utc=hb_ts + timedelta(seconds=150)))["status_code"] == "ONLINE"
            assert (await api_mod._get_laptop_worker_status(s, now_utc=hb_ts + timedelta(seconds=151)))["status_code"] == "STANDBY"
            assert (await api_mod._get_laptop_worker_status(s, now_utc=hb_ts + timedelta(seconds=299)))["status_code"] == "STANDBY"
            assert (await api_mod._get_laptop_worker_status(s, now_utc=hb_ts + timedelta(seconds=300)))["status_code"] == "STANDBY"
            off_301 = await api_mod._get_laptop_worker_status(s, now_utc=hb_ts + timedelta(seconds=301))
            assert off_301["status_code"] == "OFFLINE"
            assert off_301["is_online"] is False
    finally:
        worker_mod.reset_worker_heartbeat_cache()
        ds_mod.invalidate_dynamic_settings_cache()
        await eng.dispose()


def test_sync_engine_pool_capped_and_leak_gate_caches_miss(monkeypatch):
    # 1. _sync_engine pool arguments (1 / 1 / 1800)
    captured_kwargs: dict = {}
    fake_eng = MagicMock()

    def _fake_create_engine(url, **kwargs):
        captured_kwargs["url"] = url
        captured_kwargs.update(kwargs)
        return fake_eng

    orig_override = kr_mod._SYNC_STORE_OVERRIDE
    orig_eng = kr_mod._SYNC_ENGINE
    orig_url = kr_mod._SYNC_ENGINE_URL
    try:
        kr_mod._SYNC_STORE_OVERRIDE = "postgresql+asyncpg://u:p@db.example.com:5432/postgres"
        kr_mod._SYNC_ENGINE = None
        kr_mod._SYNC_ENGINE_URL = None
        with patch("sqlalchemy.create_engine", side_effect=_fake_create_engine):
            built = kr_mod._sync_engine()
        assert built is fake_eng
        assert captured_kwargs["pool_pre_ping"] is True
        assert captured_kwargs["pool_size"] == 1
        assert captured_kwargs["max_overflow"] == 1
        assert captured_kwargs["pool_recycle"] == 1800
    finally:
        kr_mod._SYNC_STORE_OVERRIDE = orig_override
        kr_mod._SYNC_ENGINE = orig_eng
        kr_mod._SYNC_ENGINE_URL = orig_url

    # 2. tts_leak_gate miss sentinel caching: two consecutive unset-mode reads -> 1 DB read
    leak_mod.reset_leak_gate_state_for_tests()
    clock = {"t": 500.0}
    monkeypatch.setattr(leak_mod.time, "monotonic", lambda: clock["t"])
    read_calls = {"n": 0}

    def _fake_sync_read(key: str):
        assert key == "gemini_tts_leak_gate_mode"
        read_calls["n"] += 1
        return None

    monkeypatch.setattr(kr_mod, "botsetting_sync_read", _fake_sync_read)
    try:
        m1 = leak_mod.get_leak_gate_mode()
        clock["t"] += 0.5
        m2 = leak_mod.get_leak_gate_mode()
        assert m1 == m2
        assert read_calls["n"] == 1  # Cached via _MISS_SENTINEL within 2.0 s TTL

        # After TTL expiry, re-queries once
        clock["t"] += 2.5
        leak_mod.get_leak_gate_mode()
        assert read_calls["n"] == 2
    finally:
        leak_mod.reset_leak_gate_state_for_tests()
